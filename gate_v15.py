# -*- coding: utf-8 -*-
# v15 本地门禁: 三平衡(第八门血统) + v15哨兵集 + v13/v14回归哨不回潮
import io, sys

P = 'frontend/index.html'
s = io.open(P, encoding='utf-8').read()
fails = []

def chk(name, cond, detail=''):
    print(('PASS' if cond else 'FAIL'), name, detail)
    if not cond:
        fails.append(name)

# ---- 第八门: 全文标签/花括号平衡 ----
dv_o, dv_c = s.count('<div'), s.count('</div>')
sp_o, sp_c = s.count('<span'), s.count('</span>')
br_o, br_c = s.count('{'), s.count('}')
chk('div-balance', dv_o == dv_c, '%d/%d' % (dv_o, dv_c))
chk('span-balance', sp_o == sp_c, '%d/%d' % (sp_o, sp_c))
chk('brace-balance', br_o == br_c, '%d/%d' % (br_o, br_c))

# ---- v15 哨兵集 ----
chk('dom-strip-id', s.count('id="tbOvStrip"') == 1)
chk('js-getByID-tbOvStrip', s.count("'tbOvStrip'") == 1)
chk('css-rule-main', s.count('.tb-ovstrip{') == 1)
chk('css-rule-bold', s.count('.tb-ovstrip b{') == 1)
chk('dom-class-strip', s.count('class="tb-ovstrip"') == 1)
chk('global-aggregate-loop', s.count('gg[tbEval(t).bucket]++') == 1)
chk('finished-bucket-known', "gg = { late: 0, soon: 0, ontime: 0, nosched: 0, finished: 0 }" in s)
chk('four-states-render', all(k in s for k in (
    "已超期 <b>' + gg.late",
    "临期 <b>' + gg.soon",
    "进度正常 <b>' + gg.ontime",
    "未排期 <b>' + gg.nosched")))

# ---- 回归哨(v13/v14 不回潮; 注意 span 专属定址法防 JS 注释染指) ----
chk('seg-myboard-unique', s.count('>我的任务看板<') == 1)
chk('seg-overview-unique', s.count('>任务总览<') == 1)
chk('uncond-mine-alive', 'return src.filter(tbMine)' in s)
chk('tbRefresh-called-once-at-data', s.count('tbRefresh(); //') == 1)
chk('scoped-hint-alive', "document.getElementById('tbScopeHint').textContent = '名下视野 ' + scoped.length + ' 项';" in s)

print('bytes:', len(s.encode('utf-8')))
if fails:
    print('GATE-FAIL:', ','.join(fails)); sys.exit(1)
print('GATE-ALL-GREEN')
