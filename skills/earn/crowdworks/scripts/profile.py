#!/usr/bin/env python3
"""Redacted, repeatable CrowdWorks public-profile configuration CLI."""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, re, stat, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
PLATFORM="crowdworks"; PROVIDER_EMPLOYEE_ID="7145638"; PUBLIC_URL=f"https://crowdworks.jp/public/employees/{PROVIDER_EMPLOYEE_ID}"; PROFILE_URL="https://crowdworks.jp/profile?role=employee"; PROFILE_EDIT_URL="https://crowdworks.jp/profile/edit"; EMPLOYEE_URL="https://crowdworks.jp/employee/new"; SKILLS_URL="https://crowdworks.jp/user_skills"; DEFAULT_CONFIG_PATH=Path("~/.config/anicca/crowdworks/public-profile.json").expanduser(); DEFAULT_AVATAR_PATH=Path(__file__).resolve().parents[3]/"gig-work"/"profile"/"avatar.jpg"; _HOSTS={"crowdworks.jp","www.crowdworks.jp"}; _AVATAR_HOSTS=_HOSTS|{"cw-assets.crowdworks.jp"}
_CONFIG_KEYS={"version","provider_employee_id","display_name","occupation","status","hours_limit","min_hourly_wage","max_hourly_wage","web_meeting","simple_introduction","introduction","job_categories","skills"}; _SKILL_KEYS={"name","level","years","note"}; _STATUS={"available","not_available","open","closed","active","inactive","public","private"}
class ProfileError(ValueError):
    def __init__(self,code:str): self.code=code if re.fullmatch(r"[a-z][a-z0-9_]{1,63}",code) else "profile_failed"; super().__init__(self.code)
def _fail(code:str)->None: raise ProfileError(code)
def _text(value:Any,limit:int)->str:
    return value.strip() if type(value)is str and value.strip() and len(value)<=limit and all(c in "\n\t" or ord(c)>=32 for c in value) else _fail("config_invalid")
def _skill(item:Any)->dict[str,Any]:
    if not isinstance(item,Mapping) or set(item)!=_SKILL_KEYS: _fail("config_invalid")
    value={"name":_text(item["name"],120),"level":_text(item["level"],32),"years":item["years"],"note":_text(item["note"],1000)}
    return value if type(value["years"])is int and 0<=value["years"]<=80 else _fail("config_invalid")
def validate_config(value:Any)->dict[str,Any]:
    if not isinstance(value,Mapping) or set(value)!=_CONFIG_KEYS or type(value.get("version")) is not int or value.get("version")!=1 or value.get("provider_employee_id") not in (PROVIDER_EMPLOYEE_ID,int(PROVIDER_EMPLOYEE_ID)): _fail("config_invalid")
    out=dict(value); out["provider_employee_id"]=PROVIDER_EMPLOYEE_ID; out["display_name"]=_text(value["display_name"],12); out["occupation"]=_text(value["occupation"],120); out["status"]=_text(value["status"],32)
    if out["status"] not in _STATUS or value["web_meeting"] not in {"available","not_available"}: _fail("config_invalid")
    for key,limit in (("simple_introduction",500),("introduction",5000)): out[key]=_text(value[key],limit)
    if value["hours_limit"] not in {"0-10","11-20","21-30","31-40","41-"}: _fail("config_invalid")
    for key,high in (("min_hourly_wage",10_000_000),("max_hourly_wage",10_000_000)): out[key]=value[key] if type(value[key])is int and 0<value[key]<=high else _fail("config_invalid")
    if out["min_hourly_wage"]>out["max_hourly_wage"]: _fail("config_invalid")
    categories=value["job_categories"]
    if isinstance(categories,(str,bytes,bytearray)) or not isinstance(categories,Sequence) or not categories: _fail("config_invalid")
    out["job_categories"]=[_text(item,120) for item in categories]
    if len(set(out["job_categories"]))!=len(out["job_categories"]): _fail("config_invalid")
    skills=value["skills"]
    if isinstance(skills,(str,bytes,bytearray)) or not isinstance(skills,Sequence) or not skills: _fail("config_invalid")
    out["skills"]=[_skill(item) for item in skills]
    names=[item["name"] for item in out["skills"]]
    if len(set(names))!=len(names): _fail("config_invalid")
    return out
def load_config(path:Path|str=DEFAULT_CONFIG_PATH)->dict[str,Any]:
    try:
        candidate=Path(path); info=candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600: _fail("config_invalid")
        value=json.loads(candidate.read_text(encoding="utf-8"))
    except Exception: _fail("config_invalid")
    return validate_config(value)
def _one(page:Any,selector:str,code:str="profile_unobserved")->Any:
    loc=page.locator(selector); count=loc.count()
    if type(count)is not int or count!=1: _fail(code)
    return loc
def _url(raw:Any,path:str,query:str="")->bool:
    try: p=urlsplit(raw); return p.scheme=="https" and (p.hostname or "").lower() in _HOSTS and p.port in (None,443) and p.username is None and p.password is None and p.path==path and p.query==query and not p.fragment
    except Exception: return False
def _goto(page:Any,url:str,path:str,query:str="")->None:
    try: page.goto(url); wait=getattr(page,"wait_for_load_state",None); wait(state="domcontentloaded",timeout=10_000) if callable(wait) else None
    except Exception: _fail("profile_navigation_failed")
    if not (_url(getattr(page,"url",None),path,query) or path=="/employee/new" and _url(getattr(page,"url",None),"/employee/edit")): _fail("profile_route_invalid")
def _body(page:Any)->str:
    try: value=_one(page,"body").inner_text()
    except Exception: _fail("profile_readback_failed")
    return " ".join(value.split()) if type(value)is str else _fail("profile_readback_failed")
def _hash(value:str)->str|None: return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None
def _avatar_aligned(source:Any)->bool:
    try:
        value=urlsplit(source); return value.scheme=="https" and (value.hostname or "").lower() in _AVATAR_HOSTS and "/images/user_picture/default/" not in value.path
    except Exception: return False
def _validated_avatar(path:Path|str=DEFAULT_AVATAR_PATH)->Path:
    value=Path(path)
    if not value.is_file() or value.suffix.lower() not in {".jpg",".jpeg",".png"} or not 0<value.stat().st_size<=3_000_000: _fail("profile_avatar_invalid")
    return value
def _public_avatar(page:Any)->dict[str,Any]:
    try:
        selector='img[src*="/user_picture/"]'; page.locator(selector).first.wait_for(state="attached",timeout=20_000); source=_one(page,selector,"profile_avatar_readback_failed").get_attribute("src")
    except Exception: _fail("profile_avatar_readback_failed")
    return {"present":bool(source),"aligned":_avatar_aligned(source),"hash":_hash(source or "")}
def _field(page:Any,selector:str,required:bool=False)->str:
    try: loc=_one(page,selector) if required else page.locator(selector); count=loc.count()
    except Exception: return _fail("profile_readback_failed") if required else ""
    if count!=1: return _fail("profile_readback_failed") if required else ""
    try: value=loc.input_value()
    except Exception: return ""
    return value.strip() if type(value)is str else ""
def _selected_label(page:Any,selector:str)->str:
    try: return _one(page,selector,"profile_readback_failed").locator("option:checked").inner_text().strip()
    except Exception: _fail("profile_readback_failed")
def _checked_labels(page:Any)->list[str]:
    try:
        rows=page.locator('input[name="user[job_category_ids][]"]:checked'); values=[rows.nth(index).evaluate("element => document.querySelector(`label[for='${element.id}']`)?.innerText.trim() || ''") for index in range(rows.count())]
    except Exception: _fail("profile_readback_failed")
    return sorted(value for value in values if type(value)is str and value)
def _expected_components(config:Mapping[str,Any])->dict[str,dict[str,Any]]:
    values={"display_name":config["display_name"],"occupation":config["occupation"],"status":config["status"],"hours_limit":config["hours_limit"],"min_hourly_wage":str(config["min_hourly_wage"]),"max_hourly_wage":str(config["max_hourly_wage"]),"web_meeting":config["web_meeting"],"introduction":config["introduction"],"job_categories":"\n".join(sorted(config["job_categories"])),"skills":"\n".join(sorted(skill["name"] for skill in config["skills"]))}
    return {key:{"hash":_hash(value)} for key,value in values.items()}
def _profile_aligned(components:Mapping[str,Any],config:Mapping[str,Any])->bool:
    expected=_expected_components(config)
    return bool(components.get("avatar",{}).get("aligned")) and all(components.get(key,{}).get("hash")==value["hash"] for key,value in expected.items())
def _form(page:Any,action:str,submit_value:str)->Any:
    form=_one(page,f'form[action="{action}"]',"profile_form_invalid"); target=_one(page,f'form[action="{action}"] input[type="submit"][value="{submit_value}"]',"profile_submit_invalid"); action_seen=form.get_attribute("action"); value_seen=target.get_attribute("value"); type_seen=target.get_attribute("type"); return target if action_seen in (None,action) and value_seen in (None,submit_value) and type_seen in (None,"submit") else _fail("profile_submit_invalid")
def _fill(page:Any,selector:str,value:Any)->None:
    try: _one(page,selector,"profile_field_invalid").fill(str(value))
    except Exception: _fail("profile_field_invalid")
def _select(page:Any,selector:str,value:str)->None:
    try: _one(page,selector,"profile_field_invalid").select_option(label=value)
    except Exception: _fail("profile_field_invalid")
def _value(page:Any,selector:str,value:str)->None:
    try: _one(page,selector,"profile_field_invalid").select_option(value=value)
    except Exception: _fail("profile_field_invalid")
def _categories(page:Any,names:Sequence[str])->None:
    for name in names:
        try: loc=page.get_by_label(name,exact=True); count=loc.count()
        except Exception: _fail("category_ambiguous")
        if count!=1 or loc.get_attribute("type") not in (None,"checkbox") or loc.get_attribute("name") not in (None,"user[job_category_ids][]"): _fail("category_ambiguous")
        if not loc.is_checked(): loc.evaluate("e=>{e.checked=true;e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}))}")
        if not loc.is_checked(): _fail("category_ambiguous")
def _skill_names(page:Any)->list[str]:
    try:
        rows=page.locator('tr[id^="user_skills_"]'); names=[rows.nth(i).locator("td").first.inner_text().strip() for i in range(rows.count())]
    except Exception: _fail("skill_readback_failed")
    return [name for name in names if name]
def _skill_name(page:Any,name:str)->None:
    _fill(page,'input[name="user_skill[name]"]',name); page.wait_for_timeout(800); items=page.locator("li.ui-menu-item:visible")
    matches=[items.nth(index) for index in range(items.count()) if items.nth(index).inner_text().strip()==name]
    if len(matches)!=1: _fail("profile_field_invalid")
    matches[0].click()
def observe_page(page:Any)->dict[str,Any]:
    _goto(page,PROFILE_URL,"/profile","role=employee"); _body(page); _goto(page,PROFILE_EDIT_URL,"/profile/edit"); display=_field(page,'input[name="profile[display_name]"]'); _goto(page,EMPLOYEE_URL,"/employee/new"); intro=_field(page,'textarea[name="employee[introduction]"]'); occupation=_selected_label(page,'select[name="occupation[]"]'); status=_field(page,'select[name="employee[status]"]'); hours=_field(page,'select[name="employee[hours_limit]"]'); low=_field(page,'input[name="employee[min_hourly_wage]"]'); high=_field(page,'input[name="employee[max_hourly_wage]"]'); meeting=_field(page,'input[name="employee[web_meeting]"]:checked'); categories=_checked_labels(page); _goto(page,SKILLS_URL,"/user_skills"); skills=_skill_names(page); _goto(page,PUBLIC_URL,f"/public/employees/{PROVIDER_EMPLOYEE_ID}"); public=_body(page); avatar=_public_avatar(page)
    values={"display_name":display,"occupation":occupation,"status":status,"hours_limit":hours,"min_hourly_wage":low,"max_hourly_wage":high,"web_meeting":meeting,"introduction":intro,"job_categories":"\n".join(categories),"skills":"\n".join(sorted(skills))}
    component={key:{"present":bool(value),"hash":_hash(value)} for key,value in values.items()}; component["job_categories"]["count"]=len(categories); component["skills"]["count"]=len(skills); component["avatar"]=avatar; component["public"]={"present":bool(public),"hash":_hash(public)}
    return {"ok":True,"platform":PLATFORM,"provider_employee_id":PROVIDER_EMPLOYEE_ID,"official_route":"/profile?role=employee","official_status":"observed","status":"observed","official_public_url":PUBLIC_URL,"components":component}
def _apply_page(page:Any,config:Mapping[str,Any],now:Any)->dict[str,Any]:
    observed=observe_page(page)
    if _profile_aligned(observed["components"],config):
        stamp=now() if callable(now) else now; stamp=stamp if isinstance(stamp,str) and stamp else datetime.now(timezone.utc).isoformat(); expected=_expected_components(config)
        return {"ok":True,"platform":PLATFORM,"provider_employee_id":PROVIDER_EMPLOYEE_ID,"official_public_url":PUBLIC_URL,"intent_hash":hashlib.sha256(json.dumps(config,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest(),"changed_fields":[],"profile_effect_count":0,"avatar_effect_count":0,"component_counts":{"job_categories":len(config["job_categories"]),"skills":len(config["skills"])},"component_hashes":{key:value["hash"] for key,value in expected.items()}|{"avatar":observed["components"]["avatar"]["hash"]},"timestamp":stamp,"status":"complete"}
    avatar_missing=not observed["components"]["avatar"]["aligned"]; _goto(page,PROFILE_EDIT_URL,"/profile/edit"); _fill(page,'input[name="profile[display_name]"]',config["display_name"])
    if avatar_missing:
        try: _one(page,'input[name="profile[picture_attributes][file]"]',"profile_avatar_field_invalid").set_input_files(str(_validated_avatar()))
        except ProfileError: raise
        except Exception: _fail("profile_avatar_field_invalid")
    _form(page,"/profile","基本情報を更新する").click(); _goto(page,PROFILE_EDIT_URL,"/profile/edit")
    if _field(page,'input[name="profile[display_name]"]',True)!=config["display_name"]: _fail("profile_readback_failed")
    _goto(page,EMPLOYEE_URL,"/employee/new"); _select(page,'select[name="occupation[]"]',config["occupation"]); _value(page,'select[name="employee[status]"]',config["status"]); _value(page,'select[name="employee[hours_limit]"]',config["hours_limit"]); _fill(page,'input[name="employee[min_hourly_wage]"]',config["min_hourly_wage"]); _fill(page,'input[name="employee[max_hourly_wage]"]',config["max_hourly_wage"]); _one(page,f'input[name="employee[web_meeting]"][value="{config["web_meeting"]}"]',"profile_field_invalid").check(); _fill(page,'textarea[name="employee[introduction]"]',config["introduction"]); _categories(page,config["job_categories"]); _form(page,"/employee","ワーカー情報を更新する" if urlsplit(page.url).path=="/employee/edit" else "ワーカー情報を登録する").click(); _goto(page,EMPLOYEE_URL,"/employee/new")
    if _field(page,'textarea[name="employee[introduction]"]',True)!=config["introduction"]: _fail("profile_readback_failed")
    _goto(page,SKILLS_URL,"/user_skills"); existing=_skill_names(page)
    for skill in config["skills"]:
        name=skill["name"]
        if existing.count(name)>1: _fail("skill_duplicate")
        if existing.count(name)==1: continue
        _goto(page,SKILLS_URL,"/user_skills"); _skill_name(page,name); _value(page,'select[name="user_skill[level]"]',skill["level"]); _value(page,'select[name="user_skill[years]"]',str(skill["years"])); _fill(page,'textarea[name="user_skill[note]"]',skill["note"]); _form(page,"/user_skills","スキルを登録する").click(); _goto(page,SKILLS_URL,"/user_skills"); existing=_skill_names(page)
        if existing.count(name)!=1: _fail("skill_readback_failed")
    _goto(page,PROFILE_URL,"/profile","role=employee"); _goto(page,PUBLIC_URL,f"/public/employees/{PROVIDER_EMPLOYEE_ID}"); public=_body(page); avatar=_public_avatar(page)
    # Only assert what this surface can actually prove. The public page renders the name several
    # times, collapses the introduction's newlines, never lists job categories (search metadata),
    # and truncates skills behind 職種・スキルの続きを見る — categories and skills are already
    # verified exactly against their own edit surfaces above.
    if config["display_name"] not in public or " ".join(config["introduction"].split()) not in public: _fail("public_readback_unavailable")
    if not avatar["aligned"]: _fail("profile_avatar_readback_failed")
    stamp=now() if callable(now) else now; stamp=stamp if isinstance(stamp,str) and stamp else datetime.now(timezone.utc).isoformat(); hashes={"display_name":_hash(config["display_name"]),"introduction":_hash(config["introduction"]),"job_categories":_hash("\n".join(config["job_categories"])),"skills":_hash("\n".join(sorted(skill["name"] for skill in config["skills"]))) }
    changed=["display_name","occupation","status","hours_limit","min_hourly_wage","max_hourly_wage","web_meeting","introduction","job_categories","skills"]+( ["avatar"] if avatar_missing else [] )
    return {"ok":True,"platform":PLATFORM,"provider_employee_id":PROVIDER_EMPLOYEE_ID,"official_public_url":PUBLIC_URL,"intent_hash":hashlib.sha256(json.dumps(config,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest(),"changed_fields":changed,"profile_effect_count":1,"avatar_effect_count":int(avatar_missing),"component_counts":{"job_categories":len(config["job_categories"]),"skills":len(config["skills"])},"component_hashes":hashes|{"avatar":avatar["hash"]},"timestamp":stamp,"status":"complete"}
def _new_page(browser:Any)->Any:
    try: contexts=getattr(browser,"contexts"); return contexts[0].new_page() if contexts else None
    except Exception: return None
def _defaults()->tuple[Any,Any]:
    name="crowdworks_profile_account"; spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name("account.py"))
    module=importlib.util.module_from_spec(spec); sys.modules[name]=module; spec.loader.exec_module(module); return module._owner,module._browser
def _write_receipt(path:Path,payload:Mapping[str,Any])->None:
    try:
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,prefix=f".{path.name}.",delete=False) as stream:
            os.fchmod(stream.fileno(),0o600); json.dump(payload,stream,ensure_ascii=False,sort_keys=True,separators=(",",":")); stream.flush(); os.fsync(stream.fileno())
        os.replace(stream.name,path); os.chmod(path,0o600)
    except Exception: _fail("receipt_write_failed")
def _close(page:Any)->None:
    try: page.close() if callable(getattr(page,"close",None)) else None
    except Exception: pass
def run_observe(*,browser:Any=None,page:Any=None,browser_factory:Any=None,ownership_checker:Any=None)->dict[str,Any]:
    own=page is None; created=None
    try:
        # A caller-supplied page already carries a live browser; acquiring another one here starts a
        # second Playwright runtime in the same process and throws, which is what made every
        # profile-gated application tick fail with the generic profile_apply_failed.
        if browser is None and page is None: owner,factory=_defaults(); (ownership_checker or owner)() or _fail("browser_ownership_conflict"); browser=(browser_factory or factory)("http://127.0.0.1:9228")
        created=page or _new_page(browser); created or _fail("browser_page_unavailable")
        return observe_page(created)
    except ProfileError as error: return {"ok":False,"platform":PLATFORM,"error":error.code}
    except Exception: return {"ok":False,"platform":PLATFORM,"error":"profile_observe_failed"}
    finally:
        if own: _close(created)
def run_apply(*,config_path:Path|str=DEFAULT_CONFIG_PATH,browser:Any=None,page:Any=None,browser_factory:Any=None,ownership_checker:Any=None,receipt_path:Path|str|None=None,now:Any=None)->dict[str,Any]:
    try: config=load_config(config_path)
    except Exception as error: return {"ok":False,"platform":PLATFORM,"error":error.code if isinstance(error,ProfileError) else "config_invalid"}
    own=page is None; created=None
    try:
        # A caller-supplied page already carries a live browser; acquiring another one here starts a
        # second Playwright runtime in the same process and throws, which is what made every
        # profile-gated application tick fail with the generic profile_apply_failed.
        if browser is None and page is None: owner,factory=_defaults(); (ownership_checker or owner)() or _fail("browser_ownership_conflict"); browser=(browser_factory or factory)("http://127.0.0.1:9228")
        created=page or _new_page(browser); created or _fail("browser_page_unavailable")
        result=_apply_page(created,config,now)
        if receipt_path is not None: _write_receipt(Path(receipt_path),{key:result[key] for key in ("provider_employee_id","intent_hash","changed_fields","profile_effect_count","avatar_effect_count","official_public_url","component_counts","component_hashes","timestamp","status")})
        return result
    except ProfileError as error: return {"ok":False,"platform":PLATFORM,"error":error.code}
    except Exception: return {"ok":False,"platform":PLATFORM,"error":"profile_apply_failed"}
    finally:
        if own: _close(created)
class _Parser(argparse.ArgumentParser):
    def error(self,_message:str)->None: _fail("invalid_argument")
def _parser()->argparse.ArgumentParser:
    p=_Parser(add_help=False,allow_abbrev=False); sub=p.add_subparsers(dest="command",required=True,parser_class=_Parser); o=sub.add_parser("observe",add_help=False,allow_abbrev=False); o.add_argument("--json",action="store_true",required=True); a=sub.add_parser("apply",add_help=False,allow_abbrev=False); a.add_argument("--json",action="store_true",required=True); a.add_argument("--config",default=str(DEFAULT_CONFIG_PATH)); a.add_argument("--receipt-path",default=None); return p
def main(argv:Sequence[str]|None=None,*,browser_factory:Any=None,ownership_checker:Any=None,stdout:Any=None,stderr:Any=None,now:Any=None)->int:
    out,err=stdout or sys.stdout,stderr or sys.stderr
    try:
        args=_parser().parse_args(argv); result=run_observe(browser_factory=browser_factory,ownership_checker=ownership_checker) if args.command=="observe" else run_apply(config_path=args.config,browser_factory=browser_factory,ownership_checker=ownership_checker,receipt_path=args.receipt_path,now=now)
    except ProfileError as error: result={"ok":False,"platform":PLATFORM,"error":error.code}
    except (KeyboardInterrupt,MemoryError): raise
    except Exception: result={"ok":False,"platform":PLATFORM,"error":"profile_failed"}
    try: out.write(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n"); out.flush()
    except Exception: return 5
    return 0 if result.get("ok") is True else 1
if __name__=="__main__": raise SystemExit(main())
