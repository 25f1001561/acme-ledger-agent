
import csv
import io
import os
import re
import json
import time
import calendar
import threading
from datetime import datetime
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from urllib.parse import urlparse,parse_qs,urlencode,urlunparse
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,Field

EMAIL="25f1001561@ds.study.iitm.ac.in"
ROOT="https://exam.sanand.workers.dev/questionData?email=25f1001561%40ds.study.iitm.ac.in&quizSign=XtEHz%2BqOGwxXL78O69Ln4ox6bHF4XaIjek82Df%2BnR4%2BaQRrRyorHxAIc6P6Tq7DYgFK3fflgTDTEj%2B8fLSFwI3VQLSpzfTXJ87ICQJWaBSYlMaguHk%2BdBYLJqPTp%2BVZcOTIx00wwP%2Bon9BTsjDCmB8pnpqWNjJMQv%2FJTp%2FdtztZDjNs4d5R9cgYml7pXFAYHIJ6OOhSp%2BO154aVOongtQsqujzQbOakhgUIM%2F%2BnDBmZHUFchABfGqH0pxaSmc%2Bs%2FLOp42CD%2F2oWe%2BlpXWnKjJIY1U6G%2BM4UUwV3ebYRxabXLOXi%2Fo7JHWZS9EgMUWVucVGiQIpXzK9CnTAIKL4NGzw%3D%3D&questionId=q-ledger-agent-server&path=%2F"
TZ=ZoneInfo("Asia/Kolkata")
app=FastAPI()
lock=threading.Lock()
CACHE={"expires":0,"orders":[],"rates":{},"error":None}

class Question(BaseModel):
    question:str=Field(min_length=1,max_length=2000)

def get_url(path,page=None):
    p=urlparse(ROOT)
    q=parse_qs(p.query,keep_blank_values=True)
    q["path"]=[path]
    if page is not None:
        q["page"]=[str(page)]
    else:
        q.pop("page",None)
    return urlunparse(p._replace(query=urlencode(q,doseq=True)))

def fetch(url):
    r=requests.get(url,timeout=8,headers={"Accept":"application/json,text/csv,*/*"})
    r.raise_for_status()
    text=r.text
    try:
        return r.json()
    except Exception:
        try:
            return list(csv.DictReader(io.StringIO(text)))
        except Exception:
            return text

def row_score(row):
    keys={str(k).lower() for k in row}
    score=0
    for group in [
        {"order_id","orderid","id"},
        {"status","order_status"},
        {"updated_at","updatedat"},
        {"amount","total","total_amount","revenue"},
        {"customer","customer_name","customer_id"},
        {"product","product_name","sku"},
        {"region","country","market"},
        {"order_date","date","created_at"},
    ]:
        if keys&group:
            score+=1
    return score

def extract_rows(obj):
    if isinstance(obj,list):
        dicts=[x for x in obj if isinstance(x,dict)]
        if dicts:
            return dicts
        return []
    if not isinstance(obj,dict):
        if isinstance(obj,str):
            try:
                return extract_rows(json.loads(obj))
            except Exception:
                try:
                    return list(csv.DictReader(io.StringIO(obj)))
                except Exception:
                    return []
        return []
    candidates=[]
    def walk(x,depth=0):
        if depth>8:
            return
        if isinstance(x,list):
            rows=[v for v in x if isinstance(v,dict)]
            if rows:
                candidates.append(rows)
            for v in x[:3]:
                if isinstance(v,(dict,list)):
                    walk(v,depth+1)
        elif isinstance(x,dict):
            for v in x.values():
                if isinstance(v,(dict,list)):
                    walk(v,depth+1)
    walk(obj)
    if not candidates:
        return []
    candidates.sort(key=lambda rows:(sum(row_score(r) for r in rows)/len(rows),len(rows)),reverse=True)
    return candidates[0]

def find_next(obj):
    if not isinstance(obj,dict):
        return None
    for key,value in obj.items():
        if str(key).lower() in ("next","next_page","nextpage","next_url","nexturl") and value:
            if isinstance(value,str) and value.lower() not in ("false","none","null"):
                return value
        if isinstance(value,dict):
            found=find_next(value)
            if found:
                return found
    return None

def fetch_all_orders():
    first=get_url("/orders",1)
    all_rows=[]
    seen_urls=set()
    url=first
    for page in range(1,101):
        if url in seen_urls:
            break
        seen_urls.add(url)
        obj=fetch(url)
        rows=extract_rows(obj)
        if not rows:
            break
        all_rows.extend(rows)
        nxt=find_next(obj)
        if nxt:
            url=requests.compat.urljoin(url,nxt)
            continue
        if len(rows)<100:
            break
        url=get_url("/orders",page+1)
    return all_rows

def load_data(force=False):
    with lock:
        if not force and CACHE["expires"]>time.time() and CACHE["orders"]:
            return CACHE["orders"],CACHE["rates"]
        try:
            root=fetch(ROOT)
            links=root.get("links",{}) if isinstance(root,dict) else {}
            orders_url=links.get("orders",get_url("/orders",1))
            rates_url=links.get("rates",get_url("/rates"))
            export_url=links.get("export",get_url("/export"))
            orders=[]
            try:
                orders=fetch_all_orders()
            except Exception:
                orders=[]
            try:
                exported=extract_rows(fetch(export_url))
                if len(exported)>len(orders):
                    orders=exported
            except Exception:
                pass
            rates={}
            try:
                rate_data=fetch(rates_url)
                if isinstance(rate_data,dict):
                    rates=rate_data
                else:
                    rates={"data":rate_data}
            except Exception:
                pass
            if not orders:
                raise RuntimeError("Could not retrieve ledger rows")
            dedup={}
            for row in orders:
                oid=value(row,["order_id","orderid","id","order"])
                if oid is None:
                    oid="__row_"+str(len(dedup))
                key=str(oid)
                previous=dedup.get(key)
                if previous is None or str(value(row,["updated_at","updatedat","modified_at"]) or "")>=str(value(previous,["updated_at","updatedat","modified_at"]) or ""):
                    dedup[key]=row
            CACHE.update(expires=time.time()+300,orders=list(dedup.values()),rates=rates,error=None)
        except Exception as e:
            CACHE["error"]=str(e)
            if not CACHE["orders"]:
                raise
        return CACHE["orders"],CACHE["rates"]

def norm(s):
    return re.sub(r"[^a-z0-9]+","_",str(s).lower()).strip("_")

def value(row,names,default=None):
    if not isinstance(row,dict):
        return default
    mapped={norm(k):v for k,v in row.items()}
    for name in names:
        key=norm(name)
        if key in mapped and mapped[key] not in (None,""):
            return mapped[key]
    for key,v in mapped.items():
        if any(key.endswith("_"+norm(name)) for name in names) and v not in (None,""):
            return v
    return default

def number(x):
    if x is None or isinstance(x,bool):
        return Decimal(0)
    try:
        return Decimal(str(x).replace(",","").replace("$","").strip())
    except (InvalidOperation,ValueError):
        return Decimal(0)

def as_date(x):
    if x is None:
        return None
    if isinstance(x,(int,float)):
        try:
            return datetime.fromtimestamp(x,TZ).date()
        except Exception:
            return None
    s=str(x).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z","+00:00")).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d","%d-%m-%Y","%m/%d/%Y","%Y/%m/%d","%Y-%m","%b %d %Y"):
        try:
            return datetime.strptime(s[:10] if fmt=="%Y-%m-%d" else s,fmt).date()
        except Exception:
            pass
    return None

def usd_amount(row,rates):
    amount=number(value(row,["amount_usd","total_usd","revenue_usd","usd_amount"]))
    if amount!=0:
        return amount
    amount=number(value(row,["total_amount","order_total","total","amount","revenue","grand_total","net_amount","price"]))
    currency=str(value(row,["currency","currency_code","order_currency"],"USD")).upper()
    if currency=="USD":
        return amount
    rate_map=rates
    if isinstance(rate_map,dict):
        nested=rate_map.get("rates",rate_map.get("data",rate_map))
        if isinstance(nested,dict):
            rate=number(nested.get(currency,0))
            if rate:
                return amount/rate
        if isinstance(nested,list):
            for item in nested:
                if isinstance(item,dict) and str(value(item,["currency","code","from"],"")).upper()==currency:
                    rate=number(value(item,["usd_rate","rate","to_usd","exchange_rate"]))
                    if rate:
                        return amount*rate
    return amount

def is_paid(row):
    status=str(value(row,["status","order_status","payment_status"],"paid")).lower()
    return status=="paid"

def month_window(q):
    months={m.lower():i for i,m in enumerate(calendar.month_name) if m}
    months.update({m.lower():i for i,m in enumerate(calendar.month_abbr) if m})
    m=re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b[\s,]+(20\d{2})",q,re.I)
    if m:
        month=months[m.group(1).lower()]
        year=int(m.group(2))
        return (year,month)
    m=re.search(r"\b(20\d{2})[-/](\d{1,2})\b",q)
    if m:
        return int(m.group(1)),int(m.group(2))
    return None

def filter_rows(rows,q):
    result=[]
    ql=q.lower()
    window=month_window(ql)
    year_match=re.search(r"\b(20\d{2})\b",ql)
    region=re.search(r"\b(?:in|from|for|within)\s+(?:the\s+)?([a-z][a-z -]{1,30}?)\s+region\b",ql)
    customer=re.search(r"\bcustomer\s+(?:named\s+)?['\"]?([^'\"?.,]+)",ql)
    product=re.search(r"\bproduct\s+(?:named\s+)?['\"]?([^'\"?.,]+)",ql)
    for row in rows:
        date=as_date(value(row,["order_date","date","created_at","purchase_date","ordered_at","timestamp"]))
        if window and (date is None or (date.year,date.month)!=window):
            continue
        if year_match and not window and (date is None or date.year!=int(year_match.group(1))):
            continue
        if region:
            target=region.group(1).strip().lower()
            actual=str(value(row,["region","sales_region","territory","market","customer_region"],"")).lower()
            if target not in actual:
                continue
        if customer:
            target=customer.group(1).strip().lower()
            actual=str(value(row,["customer_name","customer","customer_id","buyer","client"],"")).lower()
            if target not in actual:
                continue
        if product:
            target=product.group(1).strip().lower()
            actual=str(value(row,["product_name","product","item_name","sku","item"],"")).lower()
            if target not in actual:
                continue
        result.append(row)
    return result

def money(x):
    return float(number(x).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP))

def answer_question(question,rows,rates):
    q=question.strip()
    ql=q.lower()
    selected=filter_rows(rows,q)
    refund_words=("refund","refunded","refunds")
    product_words=("product","products","item","items","model")
    customer_words=("customer","customers","buyer","buyers","client","clients")
    count_words=("how many","count","number of","quantity","units sold","orders")
    refund=any(w in ql for w in refund_words)
    product=any(w in ql for w in product_words)
    customer=any(w in ql for w in customer_words)
    if refund:
        amounts=[]
        for row in selected:
            raw=value(row,["refund_amount_usd","refund_usd","refunded_amount","refund_amount","total_refunds","refund_total"])
            if raw is not None:
                amounts.append(number(raw))
            elif str(value(row,["status","order_status"],"")).lower() in ("refunded","partially_refunded","partially refunded"):
                amounts.append(usd_amount(row,rates))
        if any(x in ql for x in ("which","what product","top product")):
            totals={}
            for row in selected:
                name=str(value(row,["product_name","product","item_name","sku"],"Unknown"))
                raw=value(row,["refund_amount_usd","refund_usd","refunded_amount","refund_amount","total_refunds"])
                amt=number(raw) if raw is not None else (usd_amount(row,rates) if str(value(row,["status","order_status"],"")).lower() in ("refunded","partially_refunded","partially refunded") else Decimal(0))
                totals[name]=totals.get(name,Decimal(0))+amt
            return max(totals,key=totals.get) if totals else "Unknown"
        return money(sum(amounts,Decimal(0)))
    if any(w in ql for w in ("top product","best-selling product","most popular product","which product sold most","most sold product")):
        totals={}
        for row in selected:
            name=str(value(row,["product_name","product","item_name","sku","item"],"Unknown"))
            qty=number(value(row,["quantity","qty","units","items_count"],1))
            totals[name]=totals.get(name,Decimal(0))+qty
        return max(totals,key=totals.get) if totals else "Unknown"
    if any(w in ql for w in ("top customer","best customer","biggest customer","highest spending customer","who spent the most")):
        totals={}
        for row in selected:
            name=str(value(row,["customer_name","customer","customer_id","buyer","client"],"Unknown"))
            if is_paid(row):
                totals[name]=totals.get(name,Decimal(0))+usd_amount(row,rates)
        return max(totals,key=totals.get) if totals else "Unknown"
    if any(w in ql for w in ("unique customer","distinct customer","how many customers","number of customers","count customers")):
        return len({str(value(r,["customer_name","customer","customer_id","buyer","client"],"Unknown")) for r in selected})
    if any(w in ql for w in ("how many orders","number of orders","count orders","order count","total orders")):
        return sum(1 for r in selected if is_paid(r)) if "paid" in ql or "revenue" in ql else len(selected)
    if any(w in ql for w in ("units sold","quantity sold","total quantity","how many units")):
        return sum(number(value(r,["quantity","qty","units","items_count"],1)) for r in selected)
    if product and any(w in ql for w in ("list","which","what are","names","all products")):
        return sorted({str(value(r,["product_name","product","item_name","sku","item"],"Unknown")) for r in selected})
    if customer and any(w in ql for w in ("list","which","who are","names","all customers")):
        return sorted({str(value(r,["customer_name","customer","customer_id","buyer","client"],"Unknown")) for r in selected})
    if any(w in ql for w in ("average","avg","mean")):
        vals=[usd_amount(r,rates) for r in selected if is_paid(r)]
        return money(sum(vals,Decimal(0))/len(vals)) if vals else 0
    if any(w in ql for w in ("revenue","sales","total amount","total value","turnover","gross sales","total sales")):
        return money(sum((usd_amount(r,rates) for r in selected if is_paid(r)),Decimal(0)))
    if any(w in ql for w in ("which product","product name","what product")):
        totals={}
        for row in selected:
            name=str(value(row,["product_name","product","item_name","sku","item"],"Unknown"))
            totals[name]=totals.get(name,Decimal(0))+usd_amount(row,rates)
        return max(totals,key=totals.get) if totals else "Unknown"
    if any(w in ql for w in ("which customer","who is the customer","customer name")):
        totals={}
        for row in selected:
            name=str(value(row,["customer_name","customer","customer_id","buyer","client"],"Unknown"))
            totals[name]=totals.get(name,Decimal(0))+usd_amount(row,rates)
        return max(totals,key=totals.get) if totals else "Unknown"
    if "region" in ql and any(w in ql for w in ("list","which","all")):
        return sorted({str(value(r,["region","sales_region","territory","market"],"Unknown")) for r in selected})
    return {"error":"Question type not recognized"}

@app.get("/")
def health():
    return {"status":"ok","service":"Acme Appliances ledger agent"}

@app.get("/health")
def health_check():
    return {"status":"ok"}

@app.post("/")
@app.post("/answer")
@app.post("/ask")
def ask(body:Question):
    try:
        rows,rates=load_data()
        answer=answer_question(body.question,rows,rates)
        return {"answer":answer}
    except Exception:
        raise HTTPException(status_code=503,detail="Ledger data could not be loaded")
