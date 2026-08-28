"""
Sync automática periódica de todas as contas autorizadas.

Contexto: o Procfile roda `gunicorn -w 2 run:app` sem --preload, o que significa
que cada worker importa `run.py` (e portanto chama create_app()) numa cópia
separada do processo, após o fork. Se o scheduler fosse iniciado direto em
create_app() sem nenhuma trava, cada um dos 2 workers subiria seu próprio
BackgroundScheduler — resultado: cada conta seria sincronizada 2x por
intervalo, dobrando as chamadas à API do ML e piorando a contenção de escrita
no SQLite (mesmo padrão dos erros "database is locked" já vistos em produção).

Solução: lock de arquivo (flock) num arquivo no volume persistente. Só o
worker que conseguir a trava exclusiva sobe o scheduler; os demais detectam
que já tem dono e não sobem nada. Se o worker dono cair, o SO libera o flock
automaticamente e o worker de reposição que o gunicorn subir no lugar
consegue adquirir a trava na próxima tentativa.

Limitação conhecida: essa trava é por filesystem — protege corretamente
contra múltiplos workers gunicorn no mesmo container (o cenário real hoje,
replicas: 1), mas não protegeria contra múltiplas réplicas do serviço rodando
em containers/filesystems diferentes. Não é o caso atual, mas fica registrado
caso o serviço seja escalado horizontalmente no futuro.
"""
import os
import fcntl
from datetime import datetime, timedelta

from app.config import (
    DB_PATH, AUTO_SYNC_ENABLED, AUTO_SYNC_INTERVAL_HOURS,
    ADS_SYNC_ENABLED, ADS_SYNC_HOUR_UTC,
)
from app.utils.logger import get_logger

log = get_logger("scheduler")

_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".", "scheduler.lock")
_lock_file = None  # precisa ficar aberto pelo processo dono — fechar libera o flock
_scheduler = None


def _acquire_scheduler_lock() -> bool:
    """Tenta virar o worker responsável pelo scheduler. Não-bloqueante: se outro
    worker já detém a trava, retorna False imediatamente (não espera)."""
    global _lock_file
    try:
        # 'a+' não trunca no open — só o processo que efetivamente ganhar o
        # flock deve limpar/escrever o arquivo (abrir em 'w' truncaria o
        # conteúdo do dono mesmo quando é o processo perdedor que abre depois)
        _lock_file = open(_LOCK_PATH, "a+")
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file.seek(0)
        _lock_file.truncate()
        _lock_file.write(f"pid={os.getpid()} started_at={datetime.now().isoformat()}")
        _lock_file.flush()
        return True
    except OSError:
        if _lock_file:
            _lock_file.close()
            _lock_file = None
        return False


def _run_auto_sync():
    from app.services.collector import collect_all_authorized

    log.info("sync automática iniciada")
    try:
        results = collect_all_authorized()
        total_orders = sum(r.get("orders_collected", 0) for r in results)
        total_items  = sum(r.get("items_collected", 0) for r in results)
        log.info(
            f"sync automática concluída — {len(results)} conta(s), "
            f"{total_items} anúncio(s), {total_orders} pedido(s)"
        )
    except Exception as e:
        log.error(f"sync automática falhou: {e}")


def _run_ads_sync():
    from app.services.ads_collector import collect_all_ads

    log.info("sync de Ads iniciada")
    try:
        results = collect_all_ads()
        camps = sum(r.get("campaigns", 0) for r in results)
        ag_days = sum(r.get("ad_group_metric_days", 0) for r in results)
        errs = sum(len(r.get("errors", [])) for r in results)
        log.info(
            f"sync de Ads concluída — {len(results)} conta(s), {camps} campanha(s), "
            f"{ag_days} dia(s) de métrica por ad group, {errs} erro(s)"
        )
    except Exception as e:
        log.error(f"sync de Ads falhou: {e}")

    # Alertas rodam depois da coleta (dado já fresco).
    try:
        from app.services.ads_alerts import run_alerts_all
        alert_res = run_alerts_all()
        total = sum(r.get("alertas_detectados", 0) for r in alert_res)
        log.info(f"alertas de Ads: {total} detectado(s) em {len(alert_res)} conta(s)")
    except Exception as e:
        log.error(f"alertas de Ads falharam: {e}")


def start_scheduler():
    """Chamado uma vez por worker em create_app(). Só efetivamente sobe o
    scheduler no worker que conseguir a trava; nos demais, é um no-op."""
    global _scheduler

    if not (AUTO_SYNC_ENABLED or ADS_SYNC_ENABLED):
        log.info("nenhuma sync automática ligada (AUTO_SYNC_ENABLED / ADS_SYNC_ENABLED != true) "
                 "— não iniciando scheduler")
        return None

    if not _acquire_scheduler_lock():
        log.info(f"scheduler não iniciado neste worker (pid={os.getpid()}) — outro worker já detém a trava")
        return None

    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(daemon=True, timezone="UTC")

    if AUTO_SYNC_ENABLED:
        _scheduler.add_job(
            _run_auto_sync,
            "interval",
            hours=AUTO_SYNC_INTERVAL_HOURS,
            id="auto_sync",
            next_run_time=datetime.now() + timedelta(minutes=2),  # dá tempo do boot terminar
            max_instances=1,  # nunca roda dois ciclos de sync sobrepostos
            coalesce=True,    # se perder mais de um disparo (ex: processo dormiu), roda só uma vez ao voltar
        )
        log.info(f"job auto_sync: a cada {AUTO_SYNC_INTERVAL_HOURS}h, primeira em 2min")

    if ADS_SYNC_ENABLED:
        # 1x/dia, depois de 13h UTC (~10h GMT-3) — quando o ML já consolidou as
        # métricas de Ads do dia anterior.
        _scheduler.add_job(
            _run_ads_sync,
            "cron",
            hour=ADS_SYNC_HOUR_UTC,
            minute=30,
            id="ads_sync",
            max_instances=1,
            coalesce=True,
        )
        log.info(f"job ads_sync: cron diário {ADS_SYNC_HOUR_UTC:02d}:30 UTC")

    _scheduler.start()
    log.info(f"scheduler iniciado neste worker (pid={os.getpid()})")
    return _scheduler
