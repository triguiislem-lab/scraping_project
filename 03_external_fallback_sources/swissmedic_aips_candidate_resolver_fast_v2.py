#!/usr/bin/env python3
"""Targeted Swissmedic AIPS resolver for a candidate queue.

This version does not parse every AIPS document. It first builds search keys from
remaining medicines, then scans AipsDownload XML/ZIP for matching blocks and
extracts only likely document references. This is faster and avoids timeouts on
large AIPS downloads.
"""
from __future__ import annotations
import argparse, csv, hashlib, html, json, re, sys, time, unicodedata, urllib.request, zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

OUTPUT_FIELDS=["row_id","amm","nom","nom_generique","source_system","source_file","source_record_id","match_query","match_score","section_kind","section_title","section_text","language","authority_level","confidence","evidence_rank","retrieved_at","content_hash"]
STATUS_FIELDS=["row_id","amm","nom","nom_generique","dosage","forme","labo","pays","chosen_source","chosen_record_id","chosen_record_title","chosen_record_holder","chosen_language","chosen_type","chosen_html_url","chosen_pdf_url","match_score","sections","status","attempts"]
SECTION_PATTERNS=[("indication",[r"(?:4\.1\s*)?(?:Indications?|Indikationen|Indicazioni|Anwendungsgebiete)"]),("dosage",[r"(?:4\.2\s*)?(?:Dosierung|Posologie|Posologia|Art der Anwendung|Mode d.?administration|Modo di somministrazione)"]),("contraindication",[r"(?:4\.3\s*)?(?:Kontraindikationen|Contre-?indications|Controindicazioni|Contraindications)"]),("warning",[r"(?:4\.4\s*)?(?:Warnhinweise|Mises en garde|Avvertenze|Precautions|Précautions|Vorsichtsmassnahmen)"]),("interaction",[r"(?:4\.5\s*)?(?:Interaktionen|Interactions|Interazioni)"]),("special_population",[r"(?:4\.6\s*)?(?:Schwangerschaft|Stillzeit|Grossesse|Allaitement|Gravidanza|Allattamento|Fertilität|Fertilité|Fertilità)"]),("adverse_effect",[r"(?:4\.8\s*)?(?:Unerwünschte Wirkungen|Effets indésirables|Effetti indesiderati|Adverse effects)"]),("overdose",[r"(?:4\.9\s*)?(?:Überdosierung|Surdosage|Sovradosaggio|Overdose)"]),("pharmacology",[r"(?:5\.1\s*)?(?:Pharmakodynamik|Propriétés pharmacodynamiques|Proprietà farmacodinamiche)",r"(?:5\.2\s*)?(?:Pharmakokinetik|Propriétés pharmacocinétiques|Proprietà farmacocinetiche)"]),("storage",[r"(?:6\.4\s*)?(?:Besondere Lagerungshinweise|Conservation|Conservazione|Storage)"])]

def c(v:Any)->str:
    if v is None: return ""
    if isinstance(v,list): v=" ".join(c(x) for x in v)
    return " ".join(html.unescape(str(v)).replace("\ufeff","").split())

def norm(v:Any)->str:
    s=unicodedata.normalize("NFKD",str(v).upper())
    s="".join(ch for ch in s if not unicodedata.combining(ch))
    s=s.replace("®"," ").replace("™"," ").replace("µ","U")
    s="".join(ch if ("A"<=ch<="Z" or "0"<=ch<="9" or ch=="%") else " " for ch in s)
    return " ".join(s.split())

def sha(*parts:Any)->str:
    return hashlib.sha1("|".join(c(p) for p in parts).encode("utf-8","ignore")).hexdigest()

def get(tag:str,text:str)->str:
    a=f"<{tag}>"; b=f"</{tag}>"; i=text.find(a)
    if i<0: return ""
    i+=len(a); j=text.find(b,i)
    return c(text[i:j]) if j>=0 else ""

def read_csv(path:Path)->List[Dict[str,str]]:
    if not path.exists(): return []
    with path.open("r",encoding="utf-8-sig",newline="") as f:
        return [{c(k):c(v) for k,v in row.items()} for row in csv.DictReader(f)]

def write_csv(path:Path,fields:Sequence[str],rows:Iterable[Dict[str,Any]])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(fields),extrasaction="ignore"); w.writeheader()
        for row in rows: w.writerow({k:c(row.get(k,"")) for k in fields})

def build_path_candidates(raw_path:str, search_roots:Sequence[Path])->List[Path]:
    txt=c(raw_path)
    if not txt: return []
    p=Path(txt)
    if p.is_absolute(): return [p]
    out=[]; seen=set()
    for root in search_roots:
        for cand in (root/p, (root/"dpm_live_out"/p.name) if len(p.parts)==1 else None):
            if cand is None: continue
            key=str(cand)
            if key in seen: continue
            seen.add(key); out.append(cand)
    return out

def resolve_optional_input_path(raw_path:str, search_roots:Sequence[Path])->Path:
    for cand in build_path_candidates(raw_path, search_roots):
        if cand.exists(): return cand.resolve()
    return Path(raw_path)

def resolve_required_input_path(raw_path:str, search_roots:Sequence[Path])->Path:
    p=resolve_optional_input_path(raw_path, search_roots)
    if p.exists(): return p
    cands=build_path_candidates(raw_path, search_roots)
    if not cands: raise FileNotFoundError("Input file path is empty")
    shown=cands[:8]
    msg="\n".join(f"  - {x}" for x in shown)
    if len(cands)>len(shown): msg+=f"\n  - ... ({len(cands)-len(shown)} more candidate paths)"
    raise FileNotFoundError(f"Input file not found: {raw_path}\nSearched in:\n{msg}")

def load_xml(path:Path)->str:
    if not path.exists(): raise FileNotFoundError(path)
    if path.suffix.lower()==".zip":
        with zipfile.ZipFile(path,"r") as zf:
            xml_name=next((n for n in zf.namelist() if n.lower().endswith(".xml")),"")
            if not xml_name: raise RuntimeError("No XML member found in ZIP")
            with zf.open(xml_name,"r") as fh:
                return fh.read().decode("utf-8-sig","ignore")
    return path.read_text(encoding="utf-8-sig",errors="ignore")

def brand_variants(name:str)->List[str]:
    n=norm(name); out=[]
    if n: out.append(n)
    base=re.split(r"\b\d",n,1)[0].strip()
    if base and base not in out: out.append(base)
    stop={"ACIDE","VACCIN","SOLUTION","POUR","COMPRIME","INJECTABLE","HUMAINE","HUMAIN","DE","DU","DES","LA","LE","LES"}
    toks=[t for t in n.split() if len(t)>=3 and t not in stop]
    for k in (3,2,1):
        if len(toks)>=k:
            v=" ".join(toks[:k])
            if v and v not in out: out.append(v)
    return out

def active_tokens(v:str)->List[str]:
    stop={"HUMAINE","HUMAIN","PLASMATIQUE","VACCIN","CONTRE","RECOMBINANT","RECOMBINANTE","ACIDE","CHLORURE","SOLUTION"}
    return [t for t in norm(v).split() if len(t)>=4 and t not in stop][:8]

def dosage_tokens(v:str)->List[str]:
    out=[]
    for t in re.findall(r"\d+(?:[.,]\d+)?\s*(?:MG|G|ML|UI|IE|IU|U|%|MCG|UG)?",norm(v)):
        t=t.replace(" ","").replace(",",".")
        if t and t not in out: out.append(t)
    return out

def prep_row(row:Dict[str,str])->Dict[str,Any]:
    return {"variants":brand_variants(row.get("nom","")),"active":active_tokens(row.get("nom_generique","")),"lab":[t for t in norm(row.get("labo","")).split()[:8] if len(t)>=4],"dose":dosage_tokens(row.get("dosage","")+" "+row.get("nom",""))}

def load_rows(args, remaining_path:Path, medicine_summary_path:Path, covered_sections_path:Path)->List[Dict[str,str]]:
    rem=read_csv(remaining_path); summ=read_csv(medicine_summary_path); cov={r.get("row_id","") for r in read_csv(covered_sections_path) if r.get("row_id")}
    byid={r.get("row_id",""):r for r in summ}; rows=[]
    if rem:
        for r in rem:
            m=dict(r)
            for k,v in byid.get(r.get("row_id",""),{}).items():
                if v and not m.get(k): m[k]=v
            rows.append(m)
    else: rows=summ
    if args.exclude_covered: rows=[r for r in rows if r.get("row_id","") not in cov]
    if args.examples:
        needles=[norm(x) for x in args.examples.split(",") if norm(x)]
        rows=[r for r in rows if any(n in norm(r.get("nom","")) for n in needles)]
    if args.only_swissmedic_candidates:
        out=[]
        for r in rows:
            top=norm(r.get("top_source_name","")); pays=norm(r.get("pays","")); labo=norm(r.get("labo",""))
            if "SWISSMEDIC" in top or "SUISSE" in pays or "SWITZERLAND" in pays or "SUEDE" in pays or "SWED" in pays or any(x in labo for x in ["OCTAPHARMA","CSL","BEHRING","STRAGEN"]): out.append(r)
        rows=out
    return rows

def parse_block_records(b:str)->List[Dict[str,str]]:
    if get("Domain",b)!="Human": return []
    typ=get("Type",b); date=get("Date",b); auth_id=get("Identifier",get("RegulatedAuthorization",b)); holder_block=get("Holder",b); holder_id=get("Identifier",holder_block); holder=get("Name",holder_block)
    records=[]
    for ap in b.split("<AttachedDocument>")[1:]:
        a=ap.split("</AttachedDocument>",1)[0]
        lang=get("Language",a); desc=get("Description",a); period=get("Start",a); html_url=""; pdf_url=""
        for rp in a.split("<DocumentReference>")[1:]:
            rb=rp.split("</DocumentReference>",1)[0]
            ctype=get("ContentType",rb).lower(); url=get("Url",rb)
            if "html" in ctype: html_url=url
            elif "pdf" in ctype: pdf_url=url
        if desc and (html_url or pdf_url):
            tn=norm(desc); hn=norm(holder)
            records.append({"record_id":auth_id,"date":date,"type":typ,"holder_id":holder_id,"holder":holder,"language":lang,"title":desc,"period_start":period,"html_url":html_url,"pdf_url":pdf_url,"title_norm":tn,"holder_norm":hn,"title_compact":tn.replace(" ","")})
    return records

def raw_key_variants(name:str)->List[str]:
    raw=c(name).lower().replace('®',' ').replace('™',' ')
    raw=re.sub(r"\b\d.*$", "", raw).strip()
    parts=[raw]
    toks=[t for t in re.split(r"[^a-zA-Z0-9]+", raw) if len(t)>=4 and t.lower() not in {"acide","vaccin","solution","pour","humaine","humain","injectable"}]
    for k in (3,2,1):
        if len(toks)>=k: parts.append(" ".join(toks[:k]).lower())
    out=[]
    for x in parts:
        x=" ".join(x.split())
        if len(x)>=4 and x not in out: out.append(x)
    return out

def targeted_records(xml:str, rows:List[Dict[str,str]])->List[Dict[str,str]]:
    # First-pass raw lowercase search: much faster than normalizing every XML block.
    raw_keys=set()
    for row in rows:
        for k in raw_key_variants(row.get("nom","")):
            raw_keys.add(k)
    generic={"humaine","humain","vaccin","acide","solution","comprime","albumine","sodium"}
    raw_keys={k for k in raw_keys if k not in generic}
    records=[]; seen=set()
    for part in xml.split("<MedicinalDocumentsBundle>")[1:]:
        b=part.split("</MedicinalDocumentsBundle>",1)[0]
        low=b.lower()
        if not any(k in low for k in raw_keys):
            continue
        for rec in parse_block_records(b):
            sig=(rec.get("record_id"),rec.get("type"),rec.get("language"),rec.get("title"),rec.get("html_url"))
            if sig in seen: continue
            seen.add(sig); records.append(rec)
    return records

def brand_gate(ri:Dict[str,Any],rec:Dict[str,str])->bool:
    """Reject weak one-token prefix collisions such as NOVO EIGHT -> Novo-Helisen.

    Swissmedic titles are product titles, so a reliable match normally needs the
    full brand/base phrase or at least two significant brand tokens. As a narrow
    fallback, allow active+holder evidence when the brand text itself differs.
    """
    title=rec["title_norm"]; holder=rec["holder_norm"]
    variants=[v for v in ri["variants"] if v]
    # Strong phrase match: full name/base appears as phrase or title prefix.
    for v in variants[:3]:
        if len(v) >= 5 and (title==v or title.startswith(v+" ") or v in title):
            return True
    stop={"ACIDE","VACCIN","SOLUTION","POUR","COMPRIME","INJECTABLE","HUMAINE","HUMAIN","DE","DU","DES","LA","LE","LES"}
    brand_tokens=[t for t in variants[0].split() if len(t)>=3 and t not in stop] if variants else []
    title_tokens=set(title.split())
    common=sum(1 for t in brand_tokens if t in title_tokens)
    if len(brand_tokens)>=2 and common>=2:
        return True
    if len(brand_tokens)==1 and common==1 and (title==brand_tokens[0] or title.startswith(brand_tokens[0]+" ")):
        return True
    active_hits=sum(1 for t in ri["active"] if t in title)
    lab_hits=sum(1 for t in ri["lab"] if t in holder or t in title)
    return active_hits>=2 and lab_hits>=1

def score(ri:Dict[str,Any],rec:Dict[str,str])->float:
    title=rec["title_norm"]; holder=rec["holder_norm"]; comp=rec["title_compact"]; s=0.0
    if not brand_gate(ri,rec):
        return 0.0
    for i,v in enumerate(ri["variants"]):
        if title==v or title.startswith(v+" "): s=max(s,0.80-0.03*i)
        elif v and v in title: s=max(s,0.70-0.03*i)
    hits=sum(1 for t in ri["active"] if t in title)
    if hits: s+=min(0.18,0.06*hits)
    lab=sum(1 for t in ri["lab"] if t in holder or t in title)
    if lab: s+=min(0.12,0.05*lab)
    dose=sum(1 for t in ri["dose"] if t in comp)
    if dose: s+=min(0.08,0.04*dose)
    if norm(rec.get("type"))=="SMPC": s+=0.03
    return min(s,0.98)

def strip_html(page:str)->str:
    page=re.sub(r"(?is)<script.*?</script>|<style.*?</style>"," ",page)
    page=re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>","\n",page)
    return c(re.sub(r"<[^>]+>"," ",page))

def extract_sections(page:str)->List[Dict[str,str]]:
    txt=strip_html(page); matches=[]
    for kind,pats in SECTION_PATTERNS:
        for pat in pats:
            for m in re.finditer(pat,txt,flags=re.I): matches.append((m.start(),kind,c(txt[m.start():m.start()+180])))
    ded=[]
    for pos,kind,title in sorted(matches):
        if any(abs(pos-p)<25 for p,_,_ in ded): continue
        ded.append((pos,kind,title))
    out=[]; seen=set()
    for i,(pos,kind,title) in enumerate(ded):
        end=ded[i+1][0] if i+1<len(ded) else len(txt); body=c(txt[pos:min(end,pos+9000)])
        if len(body)<80: continue
        key=(kind,body[:300])
        if key in seen: continue
        seen.add(key); out.append({"section_kind":kind,"section_title":title,"section_text":body})
    return out

def fetch(url:str,timeout:int)->str:
    req=urllib.request.Request(url,headers={"User-Agent":"tunisia-cdss-swissmedic-aips/1.0","Accept":"text/html,*/*"})
    with urllib.request.urlopen(req,timeout=timeout) as r: return r.read().decode("utf-8","ignore")

def ref_sec(rec):
    txt=c(f"Swissmedic AIPS official document reference. Type: {rec.get('type')}. Title: {rec.get('title')}. Holder: {rec.get('holder')}. Authorization ID: {rec.get('record_id')}. Language: {rec.get('language')}. HTML: {rec.get('html_url')}. PDF: {rec.get('pdf_url')}.")
    return [{"section_kind":"document_reference","section_title":"Swissmedic AIPS official HTML/PDF URLs","section_text":txt}]

def sec_rows_for(row,rec,sections,source_file,sc,refonly=False):
    now=datetime.now(timezone.utc).isoformat(); out=[]; ss="swissmedic_aips_document_reference" if refonly else "swissmedic_aips_professional_info"
    for sec in sections:
        r={"row_id":row.get("row_id",""),"amm":row.get("amm",""),"nom":row.get("nom",""),"nom_generique":row.get("nom_generique",""),"source_system":ss,"source_file":source_file,"source_record_id":rec.get("record_id",""),"match_query":row.get("nom",""),"match_score":f"{sc:.2f}","section_kind":sec.get("section_kind",""),"section_title":sec.get("section_title",""),"section_text":sec.get("section_text",""),"language":rec.get("language",""),"authority_level":"fallback_swissmedic_aips","confidence":"0.62" if refonly else "0.74","evidence_rank":"61" if refonly else "75","retrieved_at":now}
        r["content_hash"]=sha(r["row_id"],r["source_system"],r["section_kind"],r["section_text"][:500]); out.append(r)
    return out

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remaining",default="remaining_382_medicines_all_available_details.csv"); ap.add_argument("--medicine-summary",default="guaranteed_source_medicine_summary.csv"); ap.add_argument("--covered-sections",default="global_regulatory_fallback_sections_cecmed_v3.csv")
    ap.add_argument("--aips-input",required=True); ap.add_argument("--output",default="swissmedic_aips_targeted_sections.csv"); ap.add_argument("--query-status-output",default="swissmedic_aips_targeted_status.csv"); ap.add_argument("--summary",default="swissmedic_aips_targeted_summary.json")
    ap.add_argument("--examples",default=""); ap.add_argument("--exclude-covered",action="store_true"); ap.add_argument("--only-swissmedic-candidates",action="store_true",default=True); ap.add_argument("--all-rows",dest="only_swissmedic_candidates",action="store_false")
    ap.add_argument("--min-score",type=float,default=0.45); ap.add_argument("--fetch-docs",action="store_true"); ap.add_argument("--emit-reference-sections",action="store_true"); ap.add_argument("--timeout",type=int,default=30); ap.add_argument("--sleep",type=float,default=0.2)
    args=ap.parse_args(); t0=time.time()
    search_roots=[Path.cwd(), Path(__file__).resolve().parent]
    remaining_path=resolve_optional_input_path(args.remaining, search_roots)
    medicine_summary_path=resolve_optional_input_path(args.medicine_summary, search_roots)
    covered_sections_path=resolve_optional_input_path(args.covered_sections, search_roots)
    aips_input_path=resolve_required_input_path(args.aips_input, search_roots)
    if not remaining_path.exists(): print(f"Warning: remaining CSV not found and treated as empty: {args.remaining}", file=sys.stderr)
    if not medicine_summary_path.exists(): print(f"Warning: medicine-summary CSV not found and treated as empty: {args.medicine_summary}", file=sys.stderr)
    if not covered_sections_path.exists(): print(f"Warning: covered-sections CSV not found and treated as empty: {args.covered_sections}", file=sys.stderr)
    rows=load_rows(args, remaining_path, medicine_summary_path, covered_sections_path); xml=load_xml(aips_input_path); records=targeted_records(xml,rows)
    def sortkey(x):
        sc,rec=x; typ=0 if norm(rec.get("type"))=="SMPC" else 1; lang={"fr":0,"de":1,"it":2,"en":3}.get(rec.get("language",""),9); return (-sc,typ,lang)
    section_rows=[]; status_rows=[]; status_counts=Counter(); source_counts=Counter()
    for row in rows:
        ri=prep_row(row); scored=[(score(ri,rec),rec) for rec in records]; scored=[x for x in scored if x[0]>=args.min_score]; scored.sort(key=sortkey)
        attempts=f"candidates={len(scored)}; targeted_records={len(records)}"; rec={}; sc=0.0; secs=[]
        if not scored:
            status="no_aips_match"; status_counts[status]+=1
        else:
            sc,rec=scored[0]; status="matched_reference"
            if args.fetch_docs and rec.get("html_url"):
                try:
                    secs=extract_sections(fetch(rec["html_url"],args.timeout)); status="ok_html_sections" if secs else "html_fetched_no_sections"
                except Exception as exc: status="html_fetch_failed:"+c(str(exc))[:120]
                if args.sleep: time.sleep(args.sleep)
            if not secs and args.emit_reference_sections: secs=ref_sec(rec)
            if secs:
                refonly=all(s.get("section_kind")=="document_reference" for s in secs)
                section_rows.extend(sec_rows_for(row,rec,secs,rec.get("html_url") or rec.get("pdf_url"),sc,refonly))
                source_counts["swissmedic_aips_document_reference" if refonly else "swissmedic_aips_professional_info"]+=1
            status_counts[status]+=1
        status_rows.append({"row_id":row.get("row_id",""),"amm":row.get("amm",""),"nom":row.get("nom",""),"nom_generique":row.get("nom_generique",""),"dosage":row.get("dosage",""),"forme":row.get("forme",""),"labo":row.get("labo",""),"pays":row.get("pays",""),"chosen_source":"swissmedic_aips" if rec else "","chosen_record_id":rec.get("record_id",""),"chosen_record_title":rec.get("title",""),"chosen_record_holder":rec.get("holder",""),"chosen_language":rec.get("language",""),"chosen_type":rec.get("type",""),"chosen_html_url":rec.get("html_url",""),"chosen_pdf_url":rec.get("pdf_url",""),"match_score":f"{sc:.2f}" if sc else "","sections":str(len(secs)),"status":status,"attempts":attempts})
    write_csv(Path(args.output),OUTPUT_FIELDS,section_rows); write_csv(Path(args.query_status_output),STATUS_FIELDS,status_rows)
    summary={"created_at":datetime.now(timezone.utc).isoformat(),"rows_processed":len(rows),"targeted_aips_records_loaded":len(records),"matched_rows":sum(1 for r in status_rows if r.get("chosen_source")),"section_rows":len(section_rows),"row_ids_with_swissmedic_sections":len({r["row_id"] for r in section_rows if r.get("section_kind")!="document_reference"}),"row_ids_with_reference_sections":len({r["row_id"] for r in section_rows if r.get("section_kind")=="document_reference"}),"status_counts":dict(status_counts),"source_row_counts":dict(source_counts),"elapsed_seconds":round(time.time()-t0,2),"outputs":{"sections":args.output,"query_status":args.query_status_output,"summary":args.summary},"notes":["AIPS ZIP/XML is a document-reference feed with official HTML/PDF URLs.","Run with --fetch-docs in an internet-enabled environment to download HTML and extract clinical sections.","document_reference rows are useful for review but should not be counted as parsed clinical sections."]}
    Path(args.summary).write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding="utf-8"); print(json.dumps(summary,indent=2,ensure_ascii=False)); return 0
if __name__=="__main__":
    raise SystemExit(main())
