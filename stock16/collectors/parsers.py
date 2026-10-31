import csv
import io
import json
import zipfile
from decimal import Decimal
from defusedxml import ElementTree as ET
from openpyxl import load_workbook
from ..model import number


def table(body,fmt,*,encoding='utf-8-sig',skip_rows=0,delimiter=',',sheet=0):
    if len(body)>25*1024*1024:
        raise ValueError('File too large')
    if fmt=='csv':
        lines=body.decode(encoding).splitlines()[skip_rows:]
        rows=[]
        for row in csv.DictReader(lines,delimiter=delimiter):
            rows.append(row)
            if len(rows)>100000: raise ValueError('Too many rows')
    elif fmt=='xlsx':
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            if len(z.infolist())>2000 or sum(f.file_size for f in z.infolist())>100*1024*1024:
                raise ValueError('Oversized XLSX archive')
        book=load_workbook(io.BytesIO(body),read_only=True,data_only=True,keep_links=False)
        try:
            worksheet=book.worksheets[sheet]
            iterator=worksheet.iter_rows(values_only=True)
            for _ in range(skip_rows): next(iterator)
            headers=[str(x).strip() for x in next(iterator)]
            rows=[]
            for r in iterator:
                rows.append(dict(zip(headers,r)))
                if len(rows)>100000: raise ValueError('Too many rows')
        finally:
            book.close()
    elif fmt=='json':
        rows=json.loads(body)
    else:
        raise ValueError('Only CSV, XLSX and structured JSON supported for tables; PDF requires reviewed extraction')
    if not isinstance(rows,list) or not rows or len(rows)>100000 or not all(isinstance(r,dict) for r in rows):
        raise ValueError('Empty or malformed table')
    return rows


def mapped_rows(rows,mapping,constants=None):
    result=[]
    for row in rows:
        item=dict(constants or {})
        for target,source in mapping.items():
            if source not in row:
                raise ValueError('Upstream columns changed; mapping needs review')
            item[target]=row[source]
        result.append(item)
    return result


def sec_information_table(body,filed_date):
    root=ET.fromstring(body)
    positions={}
    def text(node,tag,default=None):
        e=node.find('.//{*}'+tag)
        return e.text.strip() if e is not None and e.text else default
    multiplier=Decimal(1) if filed_date>='2023-01-03' else Decimal(1000)
    for node in root.findall('.//{*}infoTable'):
        cusip=text(node,'cusip')
        kind=text(node,'sshPrnamtType','SH')
        option=text(node,'putCall','')
        klass=text(node,'titleOfClass','')
        key=(cusip,kind,option,klass)
        value=Decimal(number(text(node,'value')))*multiplier
        quantity=Decimal(number(text(node,'sshPrnamt')))
        if key not in positions:
            positions[key]=dict(name=text(node,'nameOfIssuer'),cusip=cusip,
              position_key='|'.join((kind,option,klass)),shares='0' if kind=='SH' and not option else None,
              market_value='0',currency='USD',instrument='option' if option else 'equity' if kind=='SH' else 'principal')
        item=positions[key]
        if kind=='SH' and not option: item['shares']=number(Decimal(item['shares'])+quantity)
        item['market_value']=number(Decimal(item['market_value'])+value)
    if not positions:
        raise ValueError('No valid 13F information-table positions')
    total=sum(Decimal(p['market_value']) for p in positions.values())
    for p in positions.values():
        p['weight']=number(Decimal(p['market_value'])/total*100) if total else None
    return list(positions.values())
