import csv, sys, os
src, outdir = sys.argv[1], sys.argv[2]
rows = list(csv.DictReader(open(src)))
for r in rows:
    for k in ('win_hi','wilson_lb_hi','p_perm'): r[k]=float(r[k])
rows.sort(key=lambda r: -r['win_hi'])
BASE=0.380
TH={'light':dict(bg='#fcfcfb',ink='#0b0b0b',ink2='#52514e',muted='#898781',grid='#e4e3df',s='#2a78d6'),
    'dark': dict(bg='#1a1a19',ink='#f0efec',ink2='#c3c2b7',muted='#898781',grid='#2e2e2c',s='#3987e5')}
W=780; L=150; R=W-110; T=92; RH=30; H=T+RH*len(rows)+58
x0,x1=0.28,0.44
X=lambda v: L+(v-x0)/(x1-x0)*(R-L)
F='font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif"'
for mode,c in TH.items():
    o=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" {F}>',
       f'<rect width="{W}" height="{H}" rx="8" fill="{c["bg"]}"/>',
       f'<text x="24" y="34" font-size="17" font-weight="600" fill="{c["ink"]}">10 özelliğin hiçbiri temel oranı aşamadı</text>',
       f'<text x="24" y="56" font-size="12.5" fill="{c["ink2"]}">Test yarısında (n = 547) özelliği yüksek olan kırılımların kazanma oranı ve %95 Wilson alt sınırı.</text>',
       f'<text x="24" y="74" font-size="12.5" fill="{c["ink2"]}">Alt sınır temel oranın sağında olsaydı anlamlı bir avantaj olurdu. Hepsi solda kaldı.</text>']
    for t in [0.30,0.32,0.34,0.36,0.38,0.40,0.42,0.44]:
        xx=X(t); o.append(f'<line x1="{xx:.1f}" y1="{T-6}" x2="{xx:.1f}" y2="{T+RH*len(rows)}" stroke="{c["grid"]}" stroke-width="1"/>')
        o.append(f'<text x="{xx:.1f}" y="{T+RH*len(rows)+18}" font-size="11" fill="{c["muted"]}" text-anchor="middle">%{t*100:.0f}</text>')
    bx=X(BASE)
    o.append(f'<line x1="{bx:.1f}" y1="{T-10}" x2="{bx:.1f}" y2="{T+RH*len(rows)}" stroke="{c["ink2"]}" stroke-width="1.5" stroke-dasharray="4 3"/>')
    o.append(f'<text x="{bx+5:.1f}" y="{T-2}" font-size="11" fill="{c["ink2"]}">Temel oran %38,0</text>')
    o.append(f'<text x="{R+14}" y="{T-2}" font-size="11" fill="{c["muted"]}">p (perm.)</text>')
    for i,r in enumerate(rows):
        y=T+RH*i+RH/2+4
        o.append(f'<text x="{L-14}" y="{y+4:.1f}" font-size="12.5" fill="{c["ink"]}" text-anchor="end" font-family="ui-monospace,SFMono-Regular,Menlo,monospace">{r["feature"]}</text>')
        a,b=X(r['wilson_lb_hi']),X(r['win_hi'])
        o.append(f'<line x1="{a:.1f}" y1="{y:.1f}" x2="{b:.1f}" y2="{y:.1f}" stroke="{c["s"]}" stroke-width="2" stroke-linecap="round"/>')
        o.append(f'<line x1="{a:.1f}" y1="{y-6:.1f}" x2="{a:.1f}" y2="{y+6:.1f}" stroke="{c["s"]}" stroke-width="2" stroke-linecap="round"/>')
        o.append(f'<circle cx="{b:.1f}" cy="{y:.1f}" r="5" fill="{c["s"]}" stroke="{c["bg"]}" stroke-width="2"/>')
        o.append(f'<text x="{R+14}" y="{y+4:.1f}" font-size="12" fill="{c["ink2"]}">{format(r["p_perm"], ".2f").replace(".", ",")}</text>')
    ky=H-18
    o.append(f'<circle cx="30" cy="{ky-4}" r="5" fill="{c["s"]}"/><text x="42" y="{ky}" font-size="11.5" fill="{c["ink2"]}">Kazanma oranı</text>')
    o.append(f'<line x1="150" y1="{ky-10}" x2="150" y2="{ky+2}" stroke="{c["s"]}" stroke-width="2"/><text x="160" y="{ky}" font-size="11.5" fill="{c["ink2"]}">Wilson alt sınırı</text>')
    o.append(f'<text x="{W-24}" y="{ky}" font-size="11.5" fill="{c["muted"]}" text-anchor="end">Bonferroni eşiği p &lt; 0,005 · hiçbiri geçmedi</text>')
    o.append('</svg>')
    open(os.path.join(outdir,f'results-{mode}.svg'),'w').write('\n'.join(o))
