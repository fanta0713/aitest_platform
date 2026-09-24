# -*- coding: utf-8 -*-
# v16 综合门禁(文件承继自v15): 第八门三平衡 + 对称五卡十六哨 + 旧世界清零断言
# 布局:上栅五卡=tbOpen('xx','all')×4+openStatCard('issues')×1(HTML属性级精确匹配);
#       下栅五卡=tbOpen('xx')裸尾×5(与'all'版因括号终结符不同而天然区分)
import io, sys

P = 'frontend/index.html'
s = io.open(P, encoding='utf-8').read()
fails = []

def chk(name, cond, detail=''):
    print(('PASS' if cond else 'FAIL'), name, detail)
    if not cond:
        fails.append(name)

# ---- 第八门: 全文标签/花括号平衡(v3血统,DOM雪崩防波堤) ----
dv_o, dv_c = s.count('<div'), s.count('</div>')
sp_o, sp_c = s.count('<span'), s.count('</span>')
br_o, br_c = s.count('{'), s.count('}')
chk('div-balance', dv_o == dv_c, '%d/%d' % (dv_o, dv_c))
chk('span-balance', sp_o == sp_c, '%d/%d' % (sp_o, sp_c))
chk('brace-balance', br_o == br_c, '%d/%d' % (br_o, br_c))

# ---- 上栅五卡哨(全体口径) ----
chk('up-late', s.count('onclick="tbOpen(\'late\',\'all\')"') == 1)
chk('up-soon', s.count('onclick="tbOpen(\'soon\',\'all\')"') == 1)
chk('up-ontime', s.count('onclick="tbOpen(\'ontime\',\'all\')"') == 1)
chk('up-finished', s.count('onclick="tbOpen(\'finished\',\'all\')"') == 1)
chk('up-issues-htmlattr', s.count('onclick="openStatCard(\'issues\')"') == 1)
chk('id-gvLate', s.count('id="gvLate"') == 1)
chk('id-gvSoon', s.count('id="gvSoon"') == 1)
chk('id-gvOn', s.count('id="gvOn"') == 1)
chk('id-gvFin', s.count('id="gvFin"') == 1)
chk('id-gvIss', s.count('id="gvIss"') == 1)

# ---- 下栅五卡哨(名下口径;HTML属性定址,注释永不掺沙) ----
chk('dn-late', s.count('onclick="tbOpen(\'late\')"') == 1)
chk('dn-soon', s.count('onclick="tbOpen(\'soon\')"') == 1)
chk('dn-ontime', s.count('onclick="tbOpen(\'ontime\')"') == 1)
chk('dn-finished', s.count('onclick="tbOpen(\'finished\')"') == 1)
chk('dn-issues', s.count('onclick="tbOpen(\'issues\')"') == 1)
chk('id-tbLate', s.count('id="tbLate"') == 1)
chk('id-tbSoon', s.count('id="tbSoon"') == 1)
chk('id-tbOn', s.count('id="tbOn"') == 1)
chk('id-tbFin', s.count('id="tbFin"') == 1)
chk('id-tbIss', s.count('id="tbIss"') == 1)

# ---- 旧世界清零(v15摘要条/total/active/done旧数源/tbNs/nosched卡/账号FUNCTION tbGlobalView) ----
chk('dead-strip-dom', s.count('tbOvStrip') == 0)
chk('dead-ovstrip-css', s.count('tb-ovstrip') == 0)
chk('dead-statTotal', s.count('statTotal') == 0)
chk('dead-statActive', s.count('statActive') == 0)
chk('dead-statDone', s.count('statDone') == 0)
chk('dead-statIssues', s.count('statIssues') == 0)
chk('dead-tbNs', s.count('tbNs') == 0)
chk('dead-tbGlobalView-def', s.count('function tbGlobalView') == 0)
chk('dead-repeat4', s.count('repeat(4, 1fr)') == 0)
chk('live-repeat5', s.count('repeat(5, 1fr)') == 1)
chk('tasks-stats-api-cut', s.count("/api/tasks/stats") == 0)

# ---- 新心肌(v16关键逻辑哨) ----
chk('sig-tbOpen', s.count('function tbOpen(kind, scope)') == 1)
chk('logic-wide-ternary', s.count("const wide = scope === 'all';") == 1)
chk('logic-pool-branch', s.count('const pool = wide ? allTasksCache : tbScope();') == 1)
chk('logic-issues-branch', "if (kind === 'issues') { // 问题速览" in s)  # 注释指紀定址,L5361旧sf自愈分支不撞车
seg = s.split('const TB_META', 1)[1][:600]  # 只在TB_META户口本内查验,别家的issues:/finished:前驱卡位免疫
ssm = ' '.join(seg.split())
chk('meta-has-five', "finished: ['已完成任务'" in ssm and "issues: ['未关闭问题(名下任务)'" in ssm)
chk('dict-tbIssCnt-decl', s.count('let tbIssCnt = {};') == 1)
chk('dict-tbIssCnt-reset', s.count('tbIssCnt = {};') == 2)  # 声明行+loadStats进门复位
chk('dict-tbIssCnt-read', s.count('tbIssCnt[t.id]') >= 1)
chk('row-fin-badge', s.count("if (e.bucket === 'finished') dlTxt") == 1)
chk('row-iss-badge', s.count("else if (e.bucket === 'issues') dlTxt") == 1)
chk('counter-double-ledger', 'const g = { late: 0, soon: 0, ontime: 0, finished: 0 };' in s
    and 'const c = { late: 0, soon: 0, ontime: 0, finished: 0 };' in s)
chk('chained-loadstats-cure', 'loadStats();' in s)  # tbRefresh尾钩(冷启动自愈)

# ---- 回归哨(v13/v14不回潮;span专属定址防注释染指) ----
chk('seg-myboard-unique', s.count('>我的任务看板<') == 1)
chk('seg-overview-unique', s.count('>任务总览<') == 1)
chk('uncond-mine-alive', 'return src.filter(tbMine)' in s)
chk('hint-mine-alive', "'名下视野 ' + allTasksCache.filter(tbMine).length + ' 项'" in s)
chk('guard-tbLate-earlyreturn', "document.getElementById('tbLate'); if (!elLate) return;" in s)
chk('guard-cache-earlyreturn', 'if (!Array.isArray(allTasksCache)) return;' in s)

print('bytes:', len(s.encode('utf-8')))
if fails:
    print('GATE-FAIL:', ','.join(fails)); sys.exit(1)
print('GATE-ALL-GREEN')
