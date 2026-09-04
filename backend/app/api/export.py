"""
API路由 - 测试计划导出（.xlsx）

使用标准库 zipfile + 手写 OOXML 生成多 Sheet 的 Excel 文件，不依赖 openpyxl 等第三方包，
可在容器内长期稳定运行（不受容器重建影响）。
"""
import io
import zipfile
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db
from app.core.security import get_current_user
from app.models.models import User, TestTask, TaskStep, Project
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
