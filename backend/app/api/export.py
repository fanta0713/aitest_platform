"""
API路由 - 测试计划导出（.xlsx）

使用标准库 zipfile + 手写 OOXML 生成多 Sheet 的 Excel 文件，不依赖 openpyxl 等第三方包，
可在容器内长期稳定运行（不受容器重建影响）。
"""
import io
import json
import zipfile
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db
from app.core.security import get_current_user
from app.models.models import User, TestTask, TaskStep, Project, TaskCaseLink, CaseResult, TaskIssue
from app.api.cases import get_enriched_linked_cases

router = APIRouter(prefix="/api/tasks", tags=["导出"])


# ---------------- XLSX 基础构建 ----------------
def esc_xml(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#39;'))


def col_letter(idx):
    s = ''
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def sheet_xml(rows):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for ri, row in enumerate(rows, 1):
        cells = []
        for ci, cell in enumerate(row):
            if cell is None:
                continue
            kind = cell[0]
            val = cell[1]
            bold = len(cell) > 2 and cell[2]
            ref = f"{col_letter(ci)}{ri}"
            style = ' s="1"' if bold else ''
            if kind == 's':
                if val is None:
                    val = ''
                cells.append(
                    f'<c r="{ref}" t="inlineStr"{style}><is>'
                    f'<t xml:space="preserve">{esc_xml(val)}</t></is></c>')
            elif kind == 'n':
                cells.append(f'<c r="{ref}"{style}><v>{val}</v></c>')
        out.append(f'<row r="{ri}">{"".join(cells)}</row>')
    out.append('</sheetData></worksheet>')
    return ''.join(out)


CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''

ROOT_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

WORKBOOK = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>
<sheet name="任务信息" sheetId="1" r:id="rId1"/>
<sheet name="测试设计" sheetId="2" r:id="rId2"/>
</sheets>
</workbook>'''

WORKBOOK_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>'''

STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="2">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
</fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs>
</styleSheet>'''


def build_xlsx(sheet1, sheet2):
    parts = {
        '[Content_Types].xml': CONTENT_TYPES,
        '_rels/.rels': ROOT_RELS,
        'xl/workbook.xml': WORKBOOK,
        'xl/_rels/workbook.xml.rels': WORKBOOK_RELS,
        'xl/styles.xml': STYLES,
        'xl/worksheets/sheet1.xml': sheet_xml(sheet1),
        'xl/worksheets/sheet2.xml': sheet_xml(sheet2),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


# ---------------- 报表型单 Sheet 构建（列宽 + 自动换行 + 边框 + 冻结表头）----------------
# 测试跟踪记录表列多、单元格文本长，用上面那套「无列宽/无换行」的行渲染会挤成一坨，
# 故另建一套带样式的出入口；原有 build_xlsx 与其 STYLES 常量保持不动，避免影响测试计划表。

# 样式索引：0=默认 1=加粗 2=表头(加粗/灰底/居中/自动换行/边框) 3=正文(自动换行/顶端对齐/边框)
REPORT_STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="10"/><name val="微软雅黑"/></font>
<font><b/><sz val="10"/><name val="微软雅黑"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFEEF2F7"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border>
<left style="thin"><color rgb="FFD0D7E2"/></left>
<right style="thin"><color rgb="FFD0D7E2"/></right>
<top style="thin"><color rgb="FFD0D7E2"/></top>
<bottom style="thin"><color rgb="FFD0D7E2"/></bottom>
<diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
</cellXfs>
</styleSheet>'''


def _report_cell(ref, kind, val, style_idx):
    if kind == 'n':
        return f'<c r="{ref}" s="{style_idx}"><v>{val}</v></c>'
    s = '' if val is None else str(val)
    if s == '':
        return f'<c r="{ref}" s="{style_idx}"/>'
    return (f'<c r="{ref}" s="{style_idx}" t="inlineStr"><is>'
            f'<t xml:space="preserve">{esc_xml(s)}</t></is></c>')


def sheet_xml_report(rows, col_widths=None, row_heights=None, freeze_header=False):
    """报表型工作表：第 1 行为表头（样式 2），其余行正文（样式 3）"""
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if freeze_header and rows:
        out.append('<sheetViews><sheetView workbookViewId="0">'
                   '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                   '</sheetView></sheetViews>')
    out.append('<sheetFormatPr defaultRowHeight="15"/>')
    if col_widths:
        out.append('<cols>')
        for i, w in enumerate(col_widths, 1):
            out.append(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>')
        out.append('</cols>')
    out.append('<sheetData>')
    for ri, row in enumerate(rows, 1):
        style_idx = 2 if ri == 1 else 3
        cells = []
        for ci, cell in enumerate(row):
            if cell is None:
                cells.append(f'<c r="{col_letter(ci)}{ri}" s="{style_idx}"/>')
                continue
            cells.append(_report_cell(f"{col_letter(ci)}{ri}", cell[0], cell[1], style_idx))
        h = ''
        if row_heights and ri - 1 < len(row_heights) and row_heights[ri - 1]:
            h = f' ht="{row_heights[ri - 1]}" customHeight="1"'
        out.append(f'<row r="{ri}"{h}>{"".join(cells)}</row>')
    out.append('</sheetData></worksheet>')
    return ''.join(out)


def build_xlsx_single(sheet_name, rows, col_widths=None, row_heights=None, freeze_header=True):
    """单 Sheet .xlsx（报表型），sheet_name 会做 XML 转义"""
    sheet_xml_content = sheet_xml_report(rows, col_widths, row_heights, freeze_header)
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                     '</Types>')
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets><sheet name="{esc_xml(sheet_name)[:31]}" sheetId="1" r:id="rId1"/></sheets>'
                '</workbook>')
    workbook_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                     '</Relationships>')
    parts = {
        '[Content_Types].xml': content_types,
        '_rels/.rels': ROOT_RELS,
        'xl/workbook.xml': workbook,
        'xl/_rels/workbook.xml.rels': workbook_rels,
        'xl/styles.xml': REPORT_STYLES,
        'xl/worksheets/sheet1.xml': sheet_xml_content,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


# ---------------- 内容组织 ----------------
_TYPE_MAP = {'integration': '集成测试', 'functional': '功能测试',
             'performance': '性能测试', 'regression': '回归测试'}
_CASE_TYPE = {'feature': '功能', 'performance': '性能', 'integration': '集成', 'regression': '回归'}
_STATUS_MAP = {'init': '待启动', 'in_progress': '进行中', 'completed': '已完成',
               'done': '已完成', 'closed': '已关闭'}


def _fmt_date(d):
    if not d:
        return ''
    return d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)


def _fmt_dt(d):
    if not d:
        return ''
    return d.strftime('%Y-%m-%d %H:%M') if hasattr(d, 'strftime') else str(d)


def _fmt_steps(steps):
    if not steps:
        return ''
    parts = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            parts.append(f"步{i}: {s}")
            continue
        desc = s.get('desc') or s.get('step') or ''
        exp = s.get('expect') or s.get('expected') or ''
        parts.append(f"步{i}: {desc}" + (f"【期望】{exp}" if exp else ""))
    return "；".join(parts)


def build_sheet1(task, nm, project_name, test_plan_url):
    other_exec = ('、'.join(
        nm(ex.get('user_id')) for ex in (task.executors or [])
        if isinstance(ex, dict) and ex.get('user_id')) or '—')
    return [
        [('s', '测试计划表', True)],
        [],
        [('s', '字段', True), ('s', '内容', True)],
        [('s', '任务名称'), ('s', task.name or '')],
        [('s', '任务编号'), ('s', task.code or '')],
        [('s', '待测产品'), ('s', task.product_name or '')],
        [('s', '关联项目'), ('s', project_name or (str(task.project_id) if task.project_id else '—'))],
        [('s', '关联版本'), ('s', task.version or '')],
        [('s', '测试类型'), ('s', _TYPE_MAP.get(task.test_type, task.test_type or ''))],
        [('s', '开始日期'), ('s', _fmt_date(task.begin_date))],
        [('s', '结束日期'), ('s', _fmt_date(task.end_date))],
        [('s', 'PL负责人'), ('s', nm(task.owner))],
        [('s', 'TSE负责人'), ('s', nm(task.tse_id))],
        [('s', '版本负责人'), ('s', nm(task.version_owner))],
        [('s', '测试执行人'), ('s', nm(task.executor_id))],
        [('s', '其他执行人'), ('s', other_exec)],
        [('s', '任务状态'), ('s', _STATUS_MAP.get(task.status, task.status or ''))],
        [('s', '当前步骤'), ('s', f"第{task.current_step}步")],
        [('s', '进度(%)', True), ('n', task.progress or 0)],
        [('s', '任务描述'), ('s', task.description or '')],
        [('s', '创建人'), ('s', nm(task.created_by))],
        [('s', '创建时间'), ('s', _fmt_dt(task.created_at))],
        [('s', '测试方案链接'), ('s', test_plan_url or '')],
    ]


def build_sheet2(test_plan_url, cases):
    sorted_cases = sorted(
        cases, key=lambda c: (' / '.join(c.get('module_path') or []), c.get('title') or ''))
    rows = [
        [('s', '测试设计 - 测试方案与关联用例', True)],
        [],
        [('s', '测试方案链接', True), ('s', test_plan_url or '', True)],
        [],
        [('s', '序号', True), ('s', '模块路径', True), ('s', '用例ID', True),
         ('s', '用例标题', True), ('s', '类型', True), ('s', '优先级', True),
         ('s', '前置条件', True), ('s', '测试步骤', True)],
    ]
    for i, c in enumerate(sorted_cases, 1):
        rows.append([
            ('n', i),
            ('s', ' / '.join(c.get('module_path') or [])),
            ('n', c.get('id')),
            ('s', c.get('title') or ''),
            ('s', _CASE_TYPE.get(c.get('type'), c.get('type') or '')),
            ('s', f"P{c.get('priority')}" if c.get('priority') else '—'),
            ('s', c.get('precondition') or ''),
            ('s', _fmt_steps(c.get('steps'))),
        ])
    if not sorted_cases:
        rows.append([('s', '（暂无关联用例）')])
    return rows


@router.get("/{task_id}/export/test-plan")
async def export_test_plan(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """导出测试计划表（Sheet1 任务信息 / Sheet2 测试方案与关联用例）"""
    task = (await db.execute(select(TestTask).where(TestTask.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 相关人员姓名映射
    user_ids = {uid for uid in (task.owner, task.tse_id, task.version_owner,
                                task.executor_id, task.created_by) if uid}
    for ex in (task.executors or []):
        if isinstance(ex, dict) and ex.get('user_id'):
            user_ids.add(ex['user_id'])
    users = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all() \
        if user_ids else []
    name_map = {u.id: (u.realname or u.account) for u in users}
    nm = lambda uid: name_map.get(uid, str(uid) if uid else '—')

    # 步骤2 的测试方案链接
    step2 = (await db.execute(
        select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step == 2)
    )).scalar_one_or_none()
    test_plan_url = ''
    if step2 and isinstance(step2.ext_data, dict):
        test_plan_url = step2.ext_data.get('test_plan_url') or ''

    # 关联项目名称
    project_name = ''
    if task.project_id:
        proj = (await db.execute(
            select(Project).where(Project.id == task.project_id))).scalar_one_or_none()
        if proj:
            project_name = proj.name

    cases = await get_enriched_linked_cases(db, task_id)

    xlsx = build_xlsx(build_sheet1(task, nm, project_name, test_plan_url),
                      build_sheet2(test_plan_url, cases))
    filename = f"test_plan_{task.code}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------- 测试跟踪记录表（步骤7 执行测试的用例与结果汇总）----------------
_RESULT_ZH = {'pass': '通过', 'fail': '失败', 'block': '阻塞', 'na': '不支持'}
_SEVERITY_ZH = {'fatal': '致命', 'serious': '严重', 'normal': '一般', 'minor': '轻微'}
_ISSUE_STATUS_ZH = {'open': '未关闭', 'assigned': '已指派', 'fixed': '已修复',
                    'resolved': '已解决', 'closed': '已关闭', 'rejected': '已拒绝'}


def _as_dict(v):
    """steps / step_results 这类 JSON 列在 psycopg2 下可能回来是 str，统一转 dict/list"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return None
    return v


def _fmt_steps_multiline(steps):
    """步骤列：每步一行（含预期），比单行拼接更适合报表阅读"""
    if not steps or not isinstance(steps, list):
        return ''
    lines = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            lines.append(f"{i}. {s}")
            continue
        desc = s.get('desc') or s.get('step') or ''
        exp = s.get('expect') or s.get('expected') or ''
        lines.append(f"{i}. {desc}" + (f"\n   预期：{exp}" if exp else ''))
    return '\n'.join(lines)


def _build_tracking_row(case, issues_by_link, nm):
    """单条用例 → 报表行（9 列，与 TRACKING_HEADERS 严格一一对应）"""
    lr = case.get('latest_result') or {}
    status = lr.get('status') or ''
    steps = _as_dict(case.get('steps')) or []
    step_results = _as_dict(lr.get('step_results')) or []

    # 失败/阻塞原因：界面填在「失败/阻塞原因」框里，落库到 step_results[].actual
    reasons = []
    for sr in step_results if isinstance(step_results, list) else []:
        if not isinstance(sr, dict):
            continue
        if sr.get('status') in ('fail', 'block') and (sr.get('actual') or '').strip():
            idx = sr.get('step_idx')
            prefix = f"步骤{idx + 1}：" if isinstance(idx, int) else ''
            reasons.append(f"{prefix}{sr['actual'].strip()}")
    if not reasons and (lr.get('comment') or '').strip():
        reasons.append(lr['comment'].strip())

    # Bug 信息：仅失败/阻塞用例展示（2026-10-22 用户定夺）。
    # 关联关系只看 case_link_id，不管该单最终是否已修复——通过的用例即使挂过单也不带出，
    # 避免跟踪表里出现「结果=通过 却标着一条 bug」的歧义行。
    related = issues_by_link.get(case.get('id'), []) if status in ('fail', 'block') else []
    bug_parts = []
    for it in related:
        no = it.get('issue_no') or it.get('bug_no') or ''
        tags = [t for t in (it.get('severity_zh'), it.get('status_zh')) if t]
        suffix = f"（{'/'.join(tags)}）" if tags else ''
        title = it.get('title') or ''
        bug_parts.append(f"{no} {title}{suffix}".strip())

    return [
        ('s', case.get('local_case_id') or case.get('zentao_case_id') or ''),
        ('s', case.get('title') or ''),
        ('s', case.get('precondition') or ''),
        ('s', _fmt_steps_multiline(steps)),
        ('s', nm(lr.get('executor_id')) if lr.get('executor_id') else ''),
        ('s', _fmt_dt_str(lr.get('executed_at'))),
        ('s', _RESULT_ZH.get(status, status)),
        ('s', '\n'.join(reasons)),
        ('s', '\n'.join(bug_parts)),
    ]


def _fmt_dt_str(s):
    """executed_at 在 enriched 结果里是 ISO 字符串，转成 'YYYY-MM-DD HH:MM'"""
    if not s:
        return ''
    try:
        return s[:16].replace('T', ' ')
    except (TypeError, IndexError):
        return str(s)


TRACKING_HEADERS = ['ID', '用例名称', '前置条件', '步骤', '执行人', '执行时间',
                    '测试结果', '失败/阻塞原因', 'Bug信息']
# 列宽（字符数）：步骤与原因列给足，ID/结果列收窄
TRACKING_WIDTHS = [8, 34, 26, 52, 10, 17, 9, 34, 32]


@router.get("/{task_id}/export/test-tracking")
async def export_test_tracking(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """导出测试跟踪记录表：步骤7「执行测试」的用例 + 执行结果 + 失败/阻塞原因 + 关联Bug"""
    task = (await db.execute(select(TestTask).where(TestTask.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    cases = await get_enriched_linked_cases(db, task_id)

    # 执行人姓名：用例最新结果的执行人 = 实际点「确认」的人
    exec_ids = {(c.get('latest_result') or {}).get('executor_id') for c in cases}
    exec_ids = {i for i in exec_ids if i}
    users = (await db.execute(select(User).where(User.id.in_(exec_ids)))).scalars().all() \
        if exec_ids else []
    name_map = {u.id: (u.realname or u.account) for u in users}
    nm = lambda uid: name_map.get(uid, str(uid) if uid else '—')

    # 关联问题单：按 case_link_id 归组，只保留挂了单的用例
    issues = (await db.execute(
        select(TaskIssue).where(TaskIssue.task_id == task_id,
                                TaskIssue.case_link_id.isnot(None))
    )).scalars().all()
    issues_by_link = {}
    for it in issues:
        issues_by_link.setdefault(it.case_link_id, []).append({
            'issue_no': it.issue_no, 'bug_no': it.bug_no, 'title': it.title,
            'severity_zh': _SEVERITY_ZH.get(it.severity, it.severity or ''),
            'status_zh': _ISSUE_STATUS_ZH.get(it.status, it.status or ''),
        })

    rows = [[('s', h) for h in TRACKING_HEADERS]]
    if not cases:
        rows.append([('s', '') for _ in TRACKING_HEADERS])
        rows[-1][1] = ('s', '（该任务暂未关联用例）')
    else:
        for c in cases:
            row = _build_tracking_row(c, issues_by_link, nm)
            # 列数守卫：表头与数据行一旦不等长，Excel 里会整列错位且不报错
            assert len(row) == len(TRACKING_HEADERS), \
                f"列数不匹配: 表头{len(TRACKING_HEADERS)} vs 数据{len(row)}"
            rows.append(row)

    xlsx = build_xlsx_single('测试跟踪记录表', rows, TRACKING_WIDTHS)
    filename = f"test_tracking_{task.code}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
