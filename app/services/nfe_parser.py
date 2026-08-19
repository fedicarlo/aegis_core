"""Parser de NF-e (XML padrão SEFAZ) — extrai cabeçalho e itens da nota."""
import xml.etree.ElementTree as ET


class NFeParseError(Exception):
    pass


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _local_find(elem, tag_name):
    """Busca o primeiro descendente cujo tag local (sem namespace) bate com tag_name."""
    for child in elem.iter():
        if _strip_ns(child.tag) == tag_name:
            return child
    return None


def _local_findall_children(elem, tag_name):
    """Busca filhos diretos cujo tag local bate com tag_name."""
    return [c for c in elem if _strip_ns(c.tag) == tag_name]


def _text(elem, tag_name, default=None):
    node = _local_find(elem, tag_name) if elem is not None else None
    return node.text.strip() if node is not None and node.text else default


def parse_nfe_xml(xml_bytes: bytes) -> dict:
    """
    Parseia um XML de NF-e (padrão SEFAZ, com ou sem envelope nfeProc,
    com ou sem namespace) e retorna:
    {
        "chave_acesso": str,
        "supplier_cnpj": str,
        "supplier_name": str,
        "emitted_at": str | None,
        "items": [{"cprod": str, "xprod": str, "ncm": str, "qtd": float, "valor_unit": float}, ...]
    }
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise NFeParseError(f"XML inválido: {e}")

    inf_nfe = _local_find(root, "infNFe")
    if inf_nfe is None:
        raise NFeParseError("Tag <infNFe> não encontrada — não é um XML de NF-e válido.")

    chave = inf_nfe.get("Id", "")
    chave_acesso = chave[3:] if chave.upper().startswith("NFE") else chave
    if not chave_acesso:
        raise NFeParseError("Chave de acesso (atributo Id de <infNFe>) ausente.")

    emit = _local_find(inf_nfe, "emit")
    supplier_cnpj = _text(emit, "CNPJ") or _text(emit, "CPF") or ""
    supplier_name = _text(emit, "xNome") or ""
    if not supplier_cnpj:
        raise NFeParseError("CNPJ/CPF do emitente não encontrado em <emit>.")

    ide = _local_find(inf_nfe, "ide")
    emitted_at = _text(ide, "dhEmi") or _text(ide, "dEmi")

    items = []
    for det in _local_findall_children(inf_nfe, "det"):
        prod = _local_find(det, "prod")
        if prod is None:
            continue
        cprod = _text(prod, "cProd")
        xprod = _text(prod, "xProd")
        ncm = _text(prod, "NCM")
        qcom = _text(prod, "qCom")
        vuncom = _text(prod, "vUnCom")

        if not cprod:
            continue

        try:
            qtd = float(qcom) if qcom else 0.0
        except ValueError:
            qtd = 0.0
        try:
            valor_unit = float(vuncom) if vuncom else 0.0
        except ValueError:
            valor_unit = 0.0

        items.append({
            "cprod": cprod,
            "xprod": xprod or "",
            "ncm": ncm or "",
            "qtd": qtd,
            "valor_unit": valor_unit,
        })

    if not items:
        raise NFeParseError("Nenhum item (<det><prod>) encontrado na nota.")

    return {
        "chave_acesso": chave_acesso,
        "supplier_cnpj": supplier_cnpj,
        "supplier_name": supplier_name,
        "emitted_at": emitted_at,
        "items": items,
    }
