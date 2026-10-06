"""Print one bot's 4 Hz trace rows from a bot_audit_harness output.

    py -3.12 scripts/bot_audit_trace.py <run.json.gz> <bot id> <t0> <t1> [row step]
"""
import gzip,json,sys
p,bot,t0,t1=sys.argv[1],sys.argv[2],float(sys.argv[3]),float(sys.argv[4])
step=int(sys.argv[5]) if len(sys.argv)>5 else 4
r=json.load(gzip.open(p,'rt'))
rows=[x for x in r['trace'][bot] if t0<=x[0]<=t1]
for x in rows[::step]:
    print(f"t={x[0]:7.2f} a{x[1]} pos=({x[2]:.1f},{x[3]:.1f},{x[4]:.1f}) role={x[5]:<44s} act={x[6]:<6s} w{x[7]}g{x[8]} hp={x[9]} tool={x[10]} pk={x[11]} goal={x[12]} aff={x[13]} mv={x[14]} look={x[15]} blk={x[16]}")
for e in r['mode_events']:
    if t0<=e[0]<=t1: print('EV',e)
for d in r['deaths']:
    if t0<=d[0]<=t1 and (str(d[1])==bot or str(d[4])==bot): print('DEATH',d)
