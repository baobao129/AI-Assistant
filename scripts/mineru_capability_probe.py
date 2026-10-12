"""MinerU 格式能力探测（ADR-037 / CR-INGEST batch 2 任务 4）。

对每个候选格式构造**最小但结构完整**的样本，走真实的 mineru.net v4 流程
（申请上传地址 -> 预签名上传 -> 有界轮询 -> 读取结果），记录：

- 提交是否被接受（HTTP 状态与 provider 业务码）；
- 终态是 ``done`` 还是 ``failed``；
- provider 给出的分类信息（仅记录有界摘要，绝不打印凭据）。

结论用于决定 ``MINERU_SUFFIXES``：只有实测支持的格式才会进入提交前 allowlist。

用法（宿主）：``uv run python -m scripts.mineru_capability_probe``
报告写入 ``runtime/mineru_capability.json``（runtime/ 已被 Git 忽略）。
"""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from backend.infrastructure.config import Settings

POLL_INTERVAL_SECONDS = 5.0
PER_SAMPLE_TIMEOUT_SECONDS = 240.0
REPORT_PATH = Path("runtime/mineru_capability.json")

# provider 返回的业务码/状态之外，我们只保留一小段用于分类的摘要。
EXCERPT_CHARS = 160


def _zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def minimal_xlsx() -> bytes:
    """最小可打开的 OOXML 工作簿（含一个工作表与一处文本）。"""

    return _zip(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" '
                'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/officeDocument" Target="xl/workbook.xml"/>'
                "</Relationships>"
            ),
            "xl/workbook.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                "</Relationships>"
            ),
            "xl/worksheets/sheet1.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<sheetData>"
                '<row r="1"><c r="A1" t="inlineStr"><is><t>Product</t></is></c>'
                '<c r="B1" t="inlineStr"><is><t>Revenue</t></is></c></row>'
                '<row r="2"><c r="A2" t="inlineStr"><is><t>Widget</t></is></c>'
                '<c r="B2"><v>1200</v></c></row>'
                "</sheetData></worksheet>"
            ),
        }
    )


def minimal_docx() -> bytes:
    return _zip(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" '
                'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/officeDocument" Target="word/document.xml"/>'
                "</Relationships>"
            ),
            "word/document.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Capability probe paragraph.</w:t></w:r></w:p>"
                "</w:body></w:document>"
            ),
        }
    )


def minimal_pptx() -> bytes:
    """结构完整的 OOXML 演示文稿（presentation + master + layout + theme + slide）。

    只给 presentation 与 slide 不够：真实解析器需要 slideMaster/slideLayout/theme 的完整
    关系链，缺链会被判成「文件损坏」而不是「类型不支持」，无法用来证明 provider 能力。
    """

    ns = (
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    )
    empty_tree = (
        '<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
        "</p:nvGrpSpPr><p:grpSpPr/></p:spTree>"
    )
    return _zip(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" '
                'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
                '<Override PartName="/ppt/slideMasters/slideMaster1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'presentationml.slideMaster+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.presentationml.slide+xml"/>'
                '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.theme+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/officeDocument" Target="ppt/presentation.xml"/>'
                "</Relationships>"
            ),
            "ppt/presentation.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f"<p:presentation {ns}>"
                '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
                '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
                '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
                "</p:presentation>"
            ),
            "ppt/_rels/presentation.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/slide" Target="slides/slide1.xml"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/theme" Target="theme/theme1.xml"/>'
                "</Relationships>"
            ),
            "ppt/slideMasters/slideMaster1.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f"<p:sldMaster {ns}><p:cSld>{empty_tree}</p:cSld>"
                '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
                'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
                'accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
                '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
                "</p:sldMaster>"
            ),
            "ppt/slideMasters/_rels/slideMaster1.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/theme" Target="../theme/theme1.xml"/>'
                "</Relationships>"
            ),
            "ppt/slideLayouts/slideLayout1.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<p:sldLayout {ns} type="blank" preserve="1">'
                f'<p:cSld name="Blank">{empty_tree}</p:cSld>'
                "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
            ),
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
                "</Relationships>"
            ),
            "ppt/slides/slide1.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f"<p:sld {ns}><p:cSld><p:spTree>"
                '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
                "<p:grpSpPr/>"
                '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                '<p:spPr><a:xfrm><a:off x="914400" y="914400"/>'
                '<a:ext cx="7315200" cy="914400"/></a:xfrm></p:spPr>'
                "<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r>"
                "<a:t>Capability probe slide</a:t></a:r></a:p></p:txBody></p:sp>"
                "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
            ),
            "ppt/slides/_rels/slide1.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
                "</Relationships>"
            ),
            "ppt/theme/theme1.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'name="Office Theme"><a:themeElements>'
                '<a:clrScheme name="Office">'
                '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
                '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
                '<a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>'
                '<a:accent1><a:srgbClr val="4472C4"/></a:accent1>'
                '<a:accent2><a:srgbClr val="ED7D31"/></a:accent2>'
                '<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3>'
                '<a:accent4><a:srgbClr val="FFC000"/></a:accent4>'
                '<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5>'
                '<a:accent6><a:srgbClr val="70AD47"/></a:accent6>'
                '<a:hlink><a:srgbClr val="0563C1"/></a:hlink>'
                '<a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme>'
                '<a:fontScheme name="Office">'
                '<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/>'
                '<a:cs typeface=""/></a:majorFont>'
                '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/>'
                '<a:cs typeface=""/></a:minorFont></a:fontScheme>'
                '<a:fmtScheme name="Office">'
                "<a:fillStyleLst>"
                '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
                '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
                '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
                "<a:lnStyleLst>"
                '<a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
                '<a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
                '<a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
                "</a:lnStyleLst>"
                "<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle>"
                "<a:effectStyle><a:effectLst/></a:effectStyle>"
                "<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>"
                "<a:bgFillStyleLst>"
                '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
                '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
                '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>'
                "</a:fmtScheme></a:themeElements></a:theme>"
            ),
        }
    )


def minimal_html() -> bytes:
    return (
        b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        b"<title>Capability probe</title></head><body>"
        b"<h1>Capability probe</h1><table><tr><th>Product</th><th>Revenue</th></tr>"
        b"<tr><td>Widget</td><td>1200</td></tr></table></body></html>"
    )


@dataclass
class ProbeResult:
    name: str
    suffix: str
    byte_size: int
    submitted: bool
    submit_code: int | None
    terminal_state: str
    classification: str
    excerpt: str
    text_chars: int
    duration_seconds: float


def _classify(submit_code: int | None, state: str, message: str) -> str:
    lowered = message.casefold()
    if submit_code is not None and submit_code != 0:
        if "type" in lowered or "support" in lowered or "format" in lowered:
            return "unsupported_type"
        return f"submit_rejected(code={submit_code})"
    if state == "done":
        return "supported"
    if state == "failed":
        if "type" in lowered or "support" in lowered or "format" in lowered:
            return "unsupported_type"
        return "parse_failed"
    return f"state={state or 'unknown'}"


def probe(settings: Settings, *, name: str, suffix: str, payload: bytes) -> ProbeResult:
    started = time.monotonic()
    base = settings.mineru_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.mineru_api_key.get_secret_value()}"}
    filename = f"probe{name}{suffix}"

    submit_code: int | None = None
    submitted = False
    state = ""
    message = ""
    text_chars = 0
    try:
        response = httpx.post(
            f"{base}/file-urls/batch",
            headers={**headers, "Content-Type": "application/json"},
            json={"files": [{"name": filename}], "model_version": settings.mineru_model_version},
            timeout=60.0,
        )
        body = response.json()
        submit_code = int(body.get("code", -1))
        if submit_code != 0:
            message = str(body.get("msg") or body.get("message") or "")
            return ProbeResult(
                name,
                suffix,
                len(payload),
                False,
                submit_code,
                "",
                _classify(submit_code, "", message),
                message[:EXCERPT_CHARS],
                0,
                round(time.monotonic() - started, 2),
            )
        data = body.get("data") or {}
        batch_id = data.get("batch_id")
        upload_url = (data.get("file_urls") or [None])[0]
        # 预签名地址自带鉴权：附加 Authorization 会被判定为双重鉴权并 403。
        upload = httpx.put(upload_url, content=payload, timeout=120.0)
        upload.raise_for_status()
        submitted = True

        deadline = time.monotonic() + PER_SAMPLE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            poll = httpx.get(
                f"{base}/extract-results/batch/{batch_id}", headers=headers, timeout=60.0
            )
            poll.raise_for_status()
            results = (poll.json().get("data") or {}).get("extract_result") or []
            if results:
                state = str(results[0].get("state") or "")
                message = str(results[0].get("err_msg") or "")
                if state in {"done", "failed"}:
                    break
            time.sleep(POLL_INTERVAL_SECONDS)
    except (httpx.HTTPError, TypeError, ValueError) as error:
        message = type(error).__name__
        return ProbeResult(
            name,
            suffix,
            len(payload),
            submitted,
            submit_code,
            state,
            "transport_error",
            message[:EXCERPT_CHARS],
            0,
            round(time.monotonic() - started, 2),
        )

    return ProbeResult(
        name,
        suffix,
        len(payload),
        submitted,
        submit_code,
        state,
        _classify(submit_code, state, message),
        message[:EXCERPT_CHARS],
        text_chars,
        round(time.monotonic() - started, 2),
    )


def main() -> None:
    settings = Settings()
    if not settings.mineru_api_key.get_secret_value():
        raise SystemExit("MINERU_API_KEY is not configured; capability probe cannot run.")
    samples = {
        "xlsx": (".xlsx", minimal_xlsx()),
        "docx": (".docx", minimal_docx()),
        "pptx": (".pptx", minimal_pptx()),
        "html": (".html", minimal_html()),
        # 说明：`.xls` 是 BIFF8/OLE2 二进制格式，无法用 OOXML zip 手工构造出「真实样本」。
        # 这里用 OOXML 字节 + `.xls` 扩展名探测**扩展名门控**：若 provider 在提交阶段就
        # 按扩展名拒绝，则 `.xls` 明确不受支持；若被接受，则本样本内容不具代表性，
        # 结论只能是「未能证明支持」，`.xls` 继续在提交前拒绝。
        "xls": (".xls", minimal_xlsx()),
    }
    # 允许只重跑指定样本（避免为已确认的格式重复消耗 provider 额度）。
    requested = [name for name in sys.argv[1:] if name in samples]
    if requested:
        samples = {name: samples[name] for name in requested}
    results = [
        probe(settings, name=name, suffix=suffix, payload=payload)
        for name, (suffix, payload) in samples.items()
    ]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for result in results:
        print(
            f"{result.suffix:<6} bytes={result.byte_size:<7} submitted={result.submitted} "
            f"state={result.terminal_state or '-':<8} classification={result.classification}"
        )
    print(f"report: {REPORT_PATH}")


if __name__ == "__main__":
    main()
