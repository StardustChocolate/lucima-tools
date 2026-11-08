"""Check all shipped native libraries, including Chaquopy's nested archives."""
import argparse
import io
import json
from pathlib import Path
import struct
import zipfile


def check(apk):
    libraries=[];zip_failures=[]
    def scan(archive,prefix='',outer=False):
        for info in archive.infolist():
            if not info.filename.endswith(('.so','.imy')):continue
            contents=archive.read(info)
            name=prefix+info.filename
            if contents[:4]==b'\x7fELF':
                if contents[4]!=2 or contents[5]!=1:raise ValueError('Unsupported ELF format: '+name)
                phoff=struct.unpack_from('<Q',contents,32)[0]
                phentsize,phnum=struct.unpack_from('<HH',contents,54)
                loads=[];relro=[];writable=[]
                for index in range(phnum):
                    kind,flags,offset,vaddr,paddr,filesz,memsz,align=struct.unpack_from('<IIQQQQQQ',contents,phoff+index*phentsize)
                    if kind==1:
                        loads.append(align)
                        if flags & 2:writable.append((vaddr,vaddr+memsz))
                    if kind==0x6474e552:relro.append((vaddr,vaddr+memsz))
                # Android rounds RELRO protection outwards to OS pages. An
                # unaligned end is safe when the linker puts only padding up to
                # the next page; reject overlap with actual non-RELRO RW bytes.
                safe=True
                for start,end in relro:
                    protected_start=start//16384*16384
                    protected_end=(end+16383)//16384*16384
                    for low,high in writable:
                        left=max(low,protected_start);right=min(high,protected_end)
                        if left<right and (left<start or right>end):safe=False
                libraries.append(dict(file=name,min_load_alignment=min(loads),
                    relro_end_aligned=all(end%16384==0 for start,end in relro),relro_protection_safe=safe,
                    aligned=bool(loads) and min(loads)>=16384 and safe))
                if outer and info.compress_type==zipfile.ZIP_STORED:
                    with archive.open(info):pass
                    header=archive.fp
                    position=header.tell();header.seek(info.header_offset+26)
                    name_len,extra_len=struct.unpack('<HH',header.read(4));header.seek(position)
                    data_offset=info.header_offset+30+name_len+extra_len
                    if data_offset%16384:zip_failures.append(name)
            elif contents[:2]==b'PK':scan(zipfile.ZipFile(io.BytesIO(contents)),name+'!/')
    with zipfile.ZipFile(apk) as archive:scan(archive,outer=True)
    bad=[row['file'] for row in libraries if not row['aligned']]
    return dict(apk=str(Path(apk).resolve()),passed=bool(libraries) and not bad and not zip_failures,
                native_libraries=len(libraries),unaligned_libraries=bad,unaligned_zip_entries=zip_failures,libraries=libraries)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('apk');parser.add_argument('--output')
    args=parser.parse_args();result=check(args.apk)
    if args.output:Path(args.output).write_text(json.dumps(result,indent=2))
    print(json.dumps({key:value for key,value in result.items() if key!='libraries'},ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
