# -*- coding: utf-8 -*-
"""Moka 多公司招聘岗位监控：配置驱动，支持任意多家 Moka 公司页面。

用法：编辑 companies.json 增删公司即可，无需改代码。
流程：逐公司爬取近一个月岗位 -> 存快照 -> 与上次快照对比得出新增 -> 写飞书表格
      -> 周一：AI 分析 + 全量周报（每公司一条消息，全量岗位+新增提醒）
      -> 周二至周日：简易日报（单条消息，只列有新增岗位的公司；全无新增时一句话）

运行环境：
- 本机（Windows + Edge）：直接 python monitor.py 或 运行 启动招聘网页.bat
- GitHub Actions 等云端（Linux + 无头 Chrome）：自动检测 GITHUB_ACTIONS 环境变量
飞书全部使用应用自身令牌（tenant_access_token），无需浏览器 OAuth 授权，
前提：表格文档已"添加文档应用"授权给该应用；消息接收人 open_id 配置在 FEISHU_USER_OPEN_ID。
"""
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse
import re
import time
import json
import os
import glob
import requests
from dotenv import load_dotenv

load_dotenv()

IS_CI = os.getenv("GITHUB_ACTIONS") == "true"
if IS_CI:
    from selenium import webdriver
else:
    from msedge.selenium_tools import Edge, EdgeOptions

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "companies.json")
OUTPUT_DIR = os.path.join(BASE_DIR, "jobs_data")
DRIVER_PATH = os.path.join(BASE_DIR, "edgedriver_win64", "msedgedriver.exe")
EDGE_BINARY_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")
FEISHU_SPREADSHEET_TOKEN = os.getenv("FEISHU_SPREADSHEET_TOKEN")
FEISHU_USER_OPEN_ID = os.getenv("FEISHU_USER_OPEN_ID")

AI_API_URL = os.getenv("AI_API_URL") or "https://open.bigmodel.cn/api/paas/v4/chat/completions"
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL") or "glm-4-flash"


def create_driver():
    """云端：Selenium4 + 无头 Chrome；本机：Edge。"""
    if IS_CI:
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1920,1080")
        options.page_load_strategy = "eager"
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(90)
        return driver
    options = EdgeOptions()
    options.use_chromium = True
    options.binary_location = EDGE_BINARY_PATH
    options.add_argument("--start-maximized")
    return Edge(executable_path=DRIVER_PATH, options=options)


# ---------------- 配置与 URL ----------------

def load_companies():
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    companies = [c for c in config.get("companies", []) if c.get("enabled", True)]
    # priority 数字越小优先级越高（1 最高），未配置的排最后
    companies.sort(key=lambda c: c.get("priority", 99))
    if not companies:
        print("companies.json 中没有启用的公司")
    return companies


def build_url(base_url, cities=None):
    """列表页 URL。cities 为空时返回干净 URL（不带 location 参数）。

    部分公司页面（如博世校招）支持 URL 里的 location 参数，
    cities 非空时把城市过滤写进 hash；为空则只保留 page/anchor。
    """
    base = base_url.split("#")[0]
    params = []
    if cities:
        for i, city in enumerate(cities):
            params.append(f"location%5B{i}%5D={quote(city)}")
    params.append("page=1")
    params.append("anchorName=jobsList")
    return f"{base}#/jobs?{'&'.join(params)}"


# ---------------- 岗位解析 ----------------

def parse_date(date_text):
    date_str = date_text.replace("发布于 ", "").strip()
    return datetime.strptime(date_str, "%Y-%m-%d")


def is_within_one_month(job_date):
    one_month_ago = datetime.now() - timedelta(days=30)
    return job_date >= one_month_ago


def wait_for_job_list(driver, timeout=25):
    """等待岗位列表或空状态出现，返回 'jobs' / 'empty' / 'timeout'。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if driver.find_elements(By.XPATH, "//*[contains(text(), '暂无匹配职位')]"):
            return "empty"
        if driver.find_elements(By.XPATH, "//span[contains(@class, 'title-')]"):
            return "jobs"
        time.sleep(1)
    return "timeout"


def _filter_confirmed(driver, cities, timeout):
    """轮询等待"已选 N 条件"指示出现且 N 与监控城市数一致。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for el in driver.find_elements(By.XPATH, "//*[contains(text(), '已选')]"):
                m = re.search(r"已选\s*(\d+)\s*条", el.text)
                if m:
                    if int(m.group(1)) == len(cities):
                        print(f"城市筛选已生效（已选 {m.group(1)} 条件）")
                        return True
                    break
        except Exception:
            pass
        time.sleep(1)
    return False


def _click_city(driver, city):
    """点选城市复选框；若不可点击，先点"工作地点"筛选头展开下拉再重试。"""
    xpath = f"//div[contains(@class, 'no-adaptive-tooltip') and text()='{city}']"
    try:
        el = WebDriverWait(driver, 6).until(EC.element_to_be_clickable((By.XPATH, xpath)))
        el.click()
        return True
    except Exception:
        pass
    try:
        header = driver.find_element(By.XPATH, "//*[contains(text(), '工作地点')]")
        driver.execute_script("arguments[0].click();", header)
        time.sleep(1)
    except Exception:
        pass
    try:
        el = WebDriverWait(driver, 6).until(EC.element_to_be_clickable((By.XPATH, xpath)))
        el.click()
        return True
    except Exception as e:
        print(f"点选城市 {city} 失败: {e}")
        return False


def crawl_jobs(driver, company):
    name = company["name"]
    print(f"\n[{name}] 开始爬取岗位信息...")
    jobs_data = []

    job_cards = []
    deadline = time.time() + 15
    while time.time() < deadline:
        if driver.find_elements(By.XPATH, "//*[contains(text(), '暂无匹配职位')]"):
            print(f"[{name}] 无匹配岗位")
            return []
        job_cards = driver.find_elements(By.XPATH, "//span[contains(@class, 'title-')]")
        if job_cards:
            break
        time.sleep(1)
    if not job_cards:
        print(f"[{name}] 未找到岗位卡片")
        return []

    print(f"[{name}] 找到 {len(job_cards)} 个岗位")

    for i, title_span in enumerate(job_cards):
        try:
            job_name = title_span.text
            print(f"\n[{i+1}] 岗位名称: {job_name}")

            parent = title_span.find_element(By.XPATH, "./ancestor::div[contains(@class, 'card') or contains(@class, 'job') or contains(@class, 'item')][1]")

            city_text = ""
            dept_text = ""
            try:
                info_div = parent.find_element(By.XPATH, ".//div[contains(@class, 'info-')]")
                info_items = info_div.find_elements(By.XPATH, ".//div[contains(@class, 'no-adaptive-tooltip')]")
                info_texts = [t.text.strip() for t in info_items if t.text.strip()]
                # 剔除职位性质（全职/兼职/实习），剩余项倒数第1为城市、倒数第2为部门
                info_texts = [t for t in info_texts if t not in ("全职", "兼职", "实习")]
                if info_texts:
                    city_text = info_texts[-1]
                    if "·" in city_text:
                        city_text = city_text.split("·")[-1].strip()
                if len(info_texts) >= 2:
                    dept_text = info_texts[-2]
            except Exception:
                pass
            print(f"城市: {city_text}, 部门: {dept_text}")

            date_span = parent.find_element(By.XPATH, ".//span[contains(@class, 'published-at')]")
            date_text = date_span.text
            job_date = parse_date(date_text)
            print(f"发布日期: {date_text}")

            if is_within_one_month(job_date):
                try:
                    expand_btn = parent.find_element(By.XPATH, ".//span[contains(@class, 'more-icon')]")
                    driver.execute_script("arguments[0].click();", expand_btn)
                    time.sleep(1)
                except Exception:
                    pass  # 部分页面卡片默认展开，无"更多"按钮

                description_elements = parent.find_elements(By.XPATH, ".//div[@class='description' or contains(@class, 'desc')]")
                job_description = description_elements[-1].text if description_elements else ""
                if not job_description:
                    job_description = parent.text

                jobs_data.append({
                    "岗位名称": job_name,
                    "发布日期": date_text.replace("发布于 ", ""),
                    "城市": city_text,
                    "部门": dept_text,
                    "岗位描述": job_description
                })
                print("已保存（近一个月）")
            else:
                print(f"跳过（超过一个月: {job_date.strftime('%Y-%m-%d')}）")
                if i > 0:
                    break

        except Exception as e:
            print(f"处理岗位 {i+1} 时出错: {e}")

    return jobs_data


def crawl_hotjob_jobs(company):
    """大易平台（hotjob.cn）：纯 HTTP 调 listPosition 接口，无需浏览器。

    接口：POST {base}/wecruit/positionInfo/listPosition/{siteCode}?recruitType=2
    recruitType 必须放在 query/form（2=社招），放 JSON body 里后端读不到。
    """
    name = company["name"]
    cities = company.get("cities", [])
    m = re.search(r"/(SU[0-9a-f]+)/", company["url"])
    if not m:
        print(f"[{name}] URL 中未找到 siteCode（SU 开头段），跳过")
        return []
    site = m.group(1)
    base = company["url"].split(f"/{site}")[0]
    api = f"{base}/wecruit/positionInfo/listPosition/{site}"
    # filters.company：集团共享门户时按子公司名过滤（如中联重科门户里的"中科云谷"）
    company_kw = ((company.get("filters") or {}).get("company") or "").strip()
    print(f"\n[{name}] 开始爬取岗位信息（大易接口，siteCode: {site}）...")

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
               "Referer": company["url"]}
    jobs_data = []
    page, total_pages = 1, 1
    while page <= max(total_pages, 1) and page <= 50:
        try:
            # pb 门户只认 query 分页参数，mc 门户只认 form；两边都带以兼容
            resp = requests.post(api, params={"recruitType": 2, "currentPage": page, "pageSize": 20},
                                 data={"currentPage": page, "pageSize": 20},
                                 headers=headers, timeout=30)
            result = resp.json()
        except Exception as e:
            print(f"[{name}] listPosition 请求失败(第{page}页): {e}")
            break
        if result.get("state") != "200":
            print(f"[{name}] listPosition 接口异常: {result.get('msg')}")
            break
        pf = (result.get("data") or {}).get("pageForm") or {}
        total_pages = pf.get("totalPage") or 1
        data = pf.get("pageData") or []
        if not data:
            break
        print(f"[{name}] 第 {page}/{total_pages} 页，{len(data)} 个岗位")
        for it in data:
            if company_kw and company_kw not in (it.get("company") or ""):
                continue
            date_text = (it.get("publishDate") or "")[:10]
            try:
                job_date = datetime.strptime(date_text, "%Y-%m-%d")
            except ValueError:
                continue
            if not is_within_one_month(job_date):
                continue
            desc = "；".join([x for x in (
                it.get("postTypeName"), it.get("educationStr"), it.get("workYears"),
                it.get("projectName"),
                f"岗位编码 {it['postCode']}" if it.get("postCode") else "",
            ) if x])
            jobs_data.append({
                "岗位名称": it.get("postName", ""),
                "发布日期": date_text,
                "城市": it.get("workPlaceStr", ""),
                "部门": it.get("company", ""),
                "岗位描述": desc
            })
        page += 1
        time.sleep(1)

    print(f"[{name}] 接口共爬得 {len(jobs_data)} 个近一个月岗位（过滤城市前）")

    # 城市安全过滤兜底：workPlaceStr 含监控城市才保留（如"长沙市-望城区"含"长沙市"）
    if cities and jobs_data:
        jobs_data = [j for j in jobs_data if any(c == j["城市"] or c in j["城市"] for c in cities)]
        print(f"[{name}] 城市过滤后剩余 {len(jobs_data)} 个岗位")
    return jobs_data


def crawl_beisen_jobs(company):
    """北森平台（zhiye.com）：纯 HTTP 调 GetJobAdPageList 接口，无需浏览器。

    接口：POST {base}/api/Jobad/GetJobAdPageList
    - PageIndex 从 0 开始；响应 Count 为总数，Data 为当页列表
    - 必须带 DisplayFields 才回填 Category/Kind/LocNames（城市），否则全空
    - 列表接口 PostDate 恒为 0001-01-01，真实发布日期用 ChangeDate（与页面
      GetSpecialJobAdList 的 PostDate 逐条一致，2026-09-11 抽样验证）
    - companies.json 里可用 "filters": {"ClassificationTwo": ["9"]} 传门户
      侧的分类过滤（如鸣鸣很忙"总部招聘"），会合并进请求体；
      传 {"Category": "1"} 可让接口只返回社会招聘（树根互联已配）
    """
    name = company["name"]
    cities = company.get("cities", [])
    p = urlparse(company["url"])
    base = f"{p.scheme}://{p.netloc}"
    api = f"{base}/api/Jobad/GetJobAdPageList"
    filters = company.get("filters") or {}
    print(f"\n[{name}] 开始爬取岗位信息（北森接口，base: {base}，filters: {filters}）...")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Referer": company["url"],
        "Origin": base,
        "X-Requested-With": "XMLHttpRequest",
    }

    jobs_data = []
    seen_ids = set()
    total_count = None
    page = 0
    while page < 30:
        body = {
            "PageIndex": page,
            "PageSize": 50,
            "KeyWords": "",
            "SpecialType": 0,
            "PortalId": "",
            "DisplayFields": ["Category", "Kind", "LocId", "WorkWeChatQrCode"],
        }
        body.update(filters)
        try:
            resp = requests.post(api, data=json.dumps(body), headers=headers, timeout=30)
            result = resp.json()
        except Exception as e:
            print(f"[{name}] GetJobAdPageList 请求失败(第{page}页): {e}")
            break
        if result.get("Code") != 200:
            print(f"[{name}] GetJobAdPageList 接口异常: {result.get('Message')}")
            break
        total_count = result.get("Count")
        data = result.get("Data") or []
        if not data:
            break
        print(f"[{name}] 第 {page + 1} 页，{len(data)} 个岗位（总数 {total_count}）")
        for it in data:
            if it.get("Id") in seen_ids:
                continue
            seen_ids.add(it.get("Id"))
            # 北森列表接口混有校招/实习岗（如树根互联 146=社招76+校招39+实习31），
            # 与 /social/jobs 社招链接语义不符，本地按 Category 兜底剔除
            cat = (it.get("Category") or "").strip()
            if cat and "社会" not in cat:
                continue
            date_text = (it.get("ChangeDate") or "")[:10]
            try:
                job_date = datetime.strptime(date_text, "%Y-%m-%d")
            except ValueError:
                continue
            if not is_within_one_month(job_date):
                continue
            duty = (it.get("Duty") or "").strip()
            require = (it.get("Require") or "").strip()
            desc = duty[:600]
            if require:
                desc = (desc + "\n【任职要求】" + require[:400]).strip()
            tags = "；".join([x for x in (it.get("Category"), it.get("Kind")) if x])
            if tags:
                desc = f"[{tags}] " + desc
            jobs_data.append({
                "岗位名称": it.get("JobAdName", ""),
                "发布日期": date_text,
                "城市": "、".join(it.get("LocNames") or []),
                "部门": "",
                "岗位描述": desc,
            })
        if total_count and len(seen_ids) >= total_count:
            break
        page += 1
        time.sleep(1)

    print(f"[{name}] 接口共爬得 {len(jobs_data)} 个近一个月岗位（过滤城市前）")

    # 城市安全过滤兜底：LocNames 形如"湖南省·长沙市"，含监控城市才保留
    if cities and jobs_data:
        jobs_data = [j for j in jobs_data if any(c == j["城市"] or c in j["城市"] for c in cities)]
        print(f"[{name}] 城市过滤后剩余 {len(jobs_data)} 个岗位")
    return jobs_data


# ---------------- 快照与新增对比 ----------------

def load_previous_jobs(company_dir):
    files = sorted(glob.glob(os.path.join(company_dir, "jobs_*.json")))
    if not files:
        return []
    try:
        with open(files[-1], 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"读取上次快照失败: {e}")
        return []


def save_to_json(jobs_data, company_dir):
    os.makedirs(company_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(company_dir, f"jobs_{timestamp}.json")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(jobs_data, f, ensure_ascii=False, indent=2)
    print(f"数据已保存到: {filepath}")


def diff_new_jobs(current_jobs, previous_jobs):
    prev_keys = {(j.get("岗位名称"), j.get("城市")) for j in previous_jobs}
    return [j for j in current_jobs if (j.get("岗位名称"), j.get("城市")) not in prev_keys]


# ---------------- 飞书鉴权 ----------------

_tenant_token_cache = {"token": None, "expires_at": 0}


def get_tenant_token():
    """获取应用自身令牌（tenant_access_token），本地与云端通用，无需浏览器 OAuth 授权。"""
    if _tenant_token_cache["token"] and time.time() < _tenant_token_cache["expires_at"]:
        return _tenant_token_cache["token"]
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
    try:
        response = requests.post(url, json=data)
        result = response.json()
        if result.get("code") == 0:
            token = result.get("tenant_access_token")
            _tenant_token_cache["token"] = token
            _tenant_token_cache["expires_at"] = time.time() + result.get("expire", 7200) - 300
            return token
    except Exception:
        pass
    return None


# ---------------- 飞书表格 ----------------

def list_sheets(token, spreadsheet_token):
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/metainfo"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers)
        result = response.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("sheets", [])
    except Exception as e:
        print(f"读取工作表列表时出错: {e}")
    return []


def get_or_create_sheet(token, spreadsheet_token, title):
    """按标题查找工作表标签页，不存在则自动创建（v3接口），返回 sheet_id。"""
    sheets = list_sheets(token, spreadsheet_token)
    for s in sheets:
        if s.get("title") == title:
            return s.get("sheetId")

    print(f"工作表不存在，自动创建: {title}")
    url = f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"index": len(sheets), "title": title}
    try:
        response = requests.post(url, headers=headers, json=data)
        result = response.json()
        if result.get("code") == 0:
            sheet_id = result.get("data", {}).get("sheet", {}).get("sheet_id")
            if sheet_id:
                print(f"已创建工作表: {title}")
                return sheet_id
        print(f"创建工作表失败: {result.get('msg')}")
    except Exception as e:
        print(f"创建工作表时出错: {e}")
    return None


def put_values(token, spreadsheet_token, sheet_id, cell_range, rows):
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"valueRange": {"range": f"{sheet_id}!{cell_range}", "values": rows}}
    try:
        response = requests.put(url, headers=headers, json=data)
        result = response.json()
        if result.get("code") == 0:
            return True
        print(f"写入飞书表格失败({cell_range}): {result.get('msg')}")
    except Exception as e:
        print(f"写入飞书表格时出错: {e}")
    return False


def clear_sheet_area(token, spreadsheet_token, sheet_id, cols=5, max_rows=500):
    """用空字符串覆盖数据区，避免本次行数少于上次时残留旧行。"""
    to_col = "ABCDE"[cols - 1]
    empty_rows = [[""] * cols for _ in range(max_rows)]
    return put_values(token, spreadsheet_token, sheet_id, f"A2:{to_col}{max_rows + 1}", empty_rows)


def write_jobs_to_sheet(token, spreadsheet_token, sheet_id, jobs_data):
    print("\n开始写入岗位数据到飞书表格...")
    clear_sheet_area(token, spreadsheet_token, sheet_id, cols=5)
    ok = put_values(token, spreadsheet_token, sheet_id, "A1:E1",
                    [["岗位名称", "发布日期", "城市", "部门", "岗位描述"]])
    rows = [[j["岗位名称"], j["发布日期"], j["城市"], j["部门"], j["岗位描述"]] for j in jobs_data]
    if rows:
        ok = put_values(token, spreadsheet_token, sheet_id, f"A2:E{len(rows) + 1}", rows) and ok
    if ok:
        print("岗位数据成功写入飞书表格！")
    return ok


def write_analysis_to_sheet(token, spreadsheet_token, sheet_id, ai_result):
    rows = []
    for line in ai_result.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        while len(parts) < 4:
            parts.append("")
        rows.append(parts[:4])
    if not rows:
        print("AI分析结果为空，跳过写入")
        return False

    clear_sheet_area(token, spreadsheet_token, sheet_id, cols=4)
    ok = put_values(token, spreadsheet_token, sheet_id, "A1:D1",
                    [["分析项", "内容", "备注", "结论"]])
    ok = put_values(token, spreadsheet_token, sheet_id, f"A2:D{len(rows) + 1}", rows) and ok
    if ok:
        print("AI分析结果已写入飞书表格！")
    return ok


# ---------------- AI 分析与消息推送 ----------------

def ai_analyze(jobs_text, stats_info, company):
    if not AI_API_KEY:
        print("请先配置AI_API_KEY")
        return None

    name = company["name"]
    cities = company.get("cities", [])
    city_questions = "\n".join([f"{c}：主要是什么类型的部门？承担什么角色？" for c in cities])
    city_summary_hint = "，".join([f"{c}X岗" for c in cities]) if cities else "各城市X岗"

    prompt = f"""你是一个招聘数据分析专家。请对以下{name}招聘岗位数据进行分析。

{stats_info}
以上统计数据由程序精确计算，必须直接引用，禁止自行计数。

请输出两部分：

【结构化分析】
1. 岗位总量与新增
2. 城市分布
3. 岗位类型分析：按职能分类（技术/销售/运营/管理等），该公司近期主攻哪些业务方向
4. 用人需求特点：学历要求、经验年限、核心技能偏好等
5. 重点城市部门画像：
{city_questions}
每行格式为：分析项|内容|备注|结论

【推送概述】100字以内：
1. 第一句引用统计数据，格式如"{name}近一个月新发布X个岗位"
2. 城市分布用精炼格式，如"{city_summary_hint}"（引用统计数据）
3. 概括重点城市的岗位用人需求特点

岗位数据：
{jobs_text}"""

    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3
    }

    for attempt in range(3):
        try:
            print(f"正在调用AI分析...（第{attempt+1}次）")
            response = requests.post(AI_API_URL, headers=headers, json=data, timeout=120)
            result = response.json()
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                print("AI分析完成！")
                return content
            error_msg = result.get("error", {}).get("message", json.dumps(result, ensure_ascii=False)[:300])
            print(f"AI分析返回异常: {error_msg}")
            if "速率限制" in error_msg or "rate" in error_msg.lower():
                wait = 10 * (attempt + 1)
                print(f"等待{wait}秒后重试...")
                time.sleep(wait)
                continue
            return None
        except Exception as e:
            print(f"AI分析时出错: {e}")
            if attempt < 2:
                time.sleep(10)
    return None


def send_feishu_message(text):
    print("\n发送飞书消息推送...")
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        print("请先配置飞书凭证（FEISHU_APP_ID, FEISHU_APP_SECRET）")
        return False
    if not FEISHU_USER_OPEN_ID:
        print("请先配置 FEISHU_USER_OPEN_ID（消息接收人的 open_id）")
        return False

    tenant_token = get_tenant_token()
    if not tenant_token:
        print("获取tenant_access_token失败，无法发送机器人消息")
        return False

    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json"
    }
    data = {
        "receive_id": FEISHU_USER_OPEN_ID,
        "msg_type": "text",
        "content": json.dumps({"text": text})
    }
    try:
        response = requests.post(url, headers=headers, json=data)
        result = response.json()
        if result.get("code") == 0:
            print("飞书消息推送成功！")
            return True
        else:
            print(f"飞书消息推送失败: {result.get('msg')}")
            return False
    except Exception as e:
        print(f"飞书消息推送时出错: {e}")
        return False


# ---------------- 单公司完整流程 ----------------

def analyze_and_push(jobs_data, new_jobs, company):
    name = company["name"]

    if not FEISHU_SPREADSHEET_TOKEN:
        print("请先配置飞书凭证（FEISHU_SPREADSHEET_TOKEN）")
        return False

    token = get_tenant_token()
    if not token:
        return False

    # 1. 全量岗位写入 {公司名}-岗位 标签页
    jobs_sheet_id = get_or_create_sheet(token, FEISHU_SPREADSHEET_TOKEN, f"{name}-岗位")
    if jobs_sheet_id:
        write_jobs_to_sheet(token, FEISHU_SPREADSHEET_TOKEN, jobs_sheet_id, jobs_data)

    # 2. 统计信息（本地数据直接计算）
    city_count = {}
    jobs_text = ""
    for job in jobs_data:
        jobs_text += f"岗位: {job['岗位名称']}, 日期: {job['发布日期']}, 城市: {job['城市']}, 部门: {job['部门']}, 描述: {job['岗位描述']}\n"
        city = job.get("城市", "")
        if not city:
            continue
        matched = False
        for key in city_count:
            if key in city or city in key:
                city_count[key] += 1
                matched = True
                break
        if not matched:
            city_count[city] = city_count.get(city, 0) + 1

    city_summary = "，".join([f"{c}{n}岗" for c, n in city_count.items()])
    new_summary = f"，本次新增{len(new_jobs)}个岗位" if new_jobs else "，本次无新增岗位"
    stats_info = f"统计信息：近一个月共{len(jobs_data)}个岗位（{city_summary}）{new_summary}"

    # 3. AI 分析 -> {公司名}-分析 标签页
    push_summary = ""
    ai_result = ai_analyze(jobs_text, stats_info, company)
    if ai_result:
        structured = ai_result
        if "【推送概述】" in ai_result:
            parts = ai_result.split("【推送概述】")
            structured = parts[0].replace("【结构化分析】", "").strip()
            push_summary = parts[1].strip()
        analysis_sheet_id = get_or_create_sheet(token, FEISHU_SPREADSHEET_TOKEN, f"{name}-分析")
        if analysis_sheet_id:
            write_analysis_to_sheet(token, FEISHU_SPREADSHEET_TOKEN, analysis_sheet_id, structured)
    else:
        print("AI分析失败，仅推送基础岗位信息")

    # 4. 消息推送：全量分析 + 新增提醒
    message = f"📊 {name}岗位监控"
    if push_summary:
        message += f"\n{push_summary}"
    if new_jobs:
        new_list = "\n".join([f"🆕 {j['岗位名称']}（{j['发布日期']}，{j['城市']}）" for j in new_jobs])
        message += f"\n\n【本次新增 {len(new_jobs)} 个岗位】\n{new_list}"
    job_list = "\n".join([f"· {j['岗位名称']}（{j['发布日期']}，{j['城市']}）" for j in jobs_data])
    message += f"\n\n【全部岗位列表】\n{job_list}"
    return send_feishu_message(message)


def update_jobs_sheet_only(jobs_data, company):
    """非周一运行：只静默更新 {公司名}-岗位 标签页，不跑 AI、不发消息。"""
    if not FEISHU_SPREADSHEET_TOKEN:
        print("请先配置飞书凭证（FEISHU_SPREADSHEET_TOKEN）")
        return False
    token = get_tenant_token()
    if not token:
        return False
    sheet_id = get_or_create_sheet(token, FEISHU_SPREADSHEET_TOKEN, f"{company['name']}-岗位")
    if not sheet_id:
        return False
    return write_jobs_to_sheet(token, FEISHU_SPREADSHEET_TOKEN, sheet_id, jobs_data)


def send_daily_digest(results):
    """简易日报：单条消息，按优先级只列有新增岗位的公司；全无新增时一句话收尾。"""
    date_str = datetime.now().strftime("%m-%d")
    segments = []
    for r in results:
        new_jobs = r["new_jobs"]
        if not new_jobs:
            continue
        job_list = "\n".join([f"· {j['岗位名称']}（{j['发布日期']}，{j['城市']}）" for j in new_jobs])
        segments.append(f"【{r['name']}】新增 {len(new_jobs)} 个岗位\n{job_list}")
    if segments:
        message = f"📊 岗位日报 · {date_str}\n\n" + "\n\n".join(segments)
    else:
        message = f"📊 岗位日报 · {date_str}\n今日 {len(results)} 家公司均无新增岗位。"
    print(message)
    return send_feishu_message(message)


def crawl_moka_jobs(driver, company):
    """Moka 平台：浏览器打开页面 + 城市筛选 + 爬取。返回岗位列表，需跳过时返回 None。"""
    name = company["name"]
    cities = company.get("cities", [])

    url = build_url(company["url"], cities)
    print(f"打开网页: {url}")
    driver.get(url)
    time.sleep(3)

    state = wait_for_job_list(driver)
    if state == "empty":
        print(f"[{name}] 当前无匹配岗位，跳过飞书同步")
        return None

    if state == "timeout":
        print(f"[{name}] 页面加载超时，尝试继续处理...")

    # 方案一：URL location 参数（博世校招等页面支持），用"已选 N 条件"指示确认
    filter_ok = not cities or _filter_confirmed(driver, cities, timeout=8)

    # 方案二：部分页面忽略 URL 参数且带参数时点选也失效（如施耐德社招），
    # 必须用干净 URL 重新加载后再点选城市复选框
    if cities and not filter_ok:
        clean_url = build_url(company["url"], [])
        print(f"URL 城市过滤未生效，改用干净 URL 重新加载后点选: {clean_url}")
        driver.get(clean_url)
        time.sleep(3)
        state = wait_for_job_list(driver)
        if state == "empty":
            print(f"[{name}] 当前无匹配岗位，跳过飞书同步")
            return None
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//*[contains(text(), '工作地点')]"))
            )
        except Exception:
            pass
        for city in cities:
            _click_city(driver, city)
            time.sleep(1)
        if not _filter_confirmed(driver, cities, timeout=10):
            print("警告：未能确认城市筛选状态，将依赖安全过滤兜底")

    time.sleep(2)
    jobs_data = crawl_jobs(driver, company)

    # 安全过滤：即使页面过滤失效，也只保留监控城市范围内的岗位
    if cities and jobs_data:
        filtered = [j for j in jobs_data if any(c == j.get("城市") or c in (j.get("城市") or "") for c in cities)]
        dropped = len(jobs_data) - len(filtered)
        if dropped:
            print(f"[{name}] 安全过滤：剔除 {dropped} 个不在监控城市范围内的岗位")
        jobs_data = filtered

    if not jobs_data:
        print(f"[{name}] 无近一个月内发布的岗位，跳过飞书同步")
        return None
    return jobs_data


def process_company(driver, company, is_monday):
    cid = company["id"]
    name = company["name"]
    cities = company.get("cities", [])
    company_dir = os.path.join(OUTPUT_DIR, cid)

    platform = company.get("platform", "moka")
    print(f"\n{'='*60}\n开始处理: {name}（平台: {platform}，城市: {'、'.join(cities) if cities else '不限'}）\n{'='*60}")

    try:
        if platform == "hotjob":
            jobs_data = crawl_hotjob_jobs(company)
            if not jobs_data:
                print(f"[{name}] 无近一个月内发布的岗位，跳过飞书同步")
                return {"name": name, "new_jobs": []}
        elif platform == "beisen":
            jobs_data = crawl_beisen_jobs(company)
            if not jobs_data:
                print(f"[{name}] 无近一个月内发布的岗位，跳过飞书同步")
                return {"name": name, "new_jobs": []}
        else:
            jobs_data = crawl_moka_jobs(driver, company)
            if jobs_data is None:
                return {"name": name, "new_jobs": []}

        previous_jobs = load_previous_jobs(company_dir)
        new_jobs = diff_new_jobs(jobs_data, previous_jobs)
        print(f"\n[{name}] 共 {len(jobs_data)} 个岗位，本次新增 {len(new_jobs)} 个")

        save_to_json(jobs_data, company_dir)
        if is_monday:
            analyze_and_push(jobs_data, new_jobs, company)
        else:
            update_jobs_sheet_only(jobs_data, company)
        return {"name": name, "new_jobs": new_jobs}

    except Exception as e:
        print(f"[{name}] 处理出错: {e}")
        return None


# ---------------- 主流程 ----------------

def main():
    companies = load_companies()
    if not companies:
        return
    print(f"本次监控 {len(companies)} 家公司: {'、'.join(c['name'] for c in companies)}")

    # 周一推全量周报（AI 分析+每公司一条）；周二至周日推简易日报（仅新增岗位，不调 AI）
    is_monday = datetime.now().weekday() == 0
    print("今天是周一，推送全量周报" if is_monday else "非周一，推送简易日报（仅新增岗位）")

    # 仅 Moka 公司需要浏览器；全走接口（hotjob/beisen 等）时不启动
    moka_companies = [c for c in companies if c.get("platform", "moka") not in ("hotjob", "beisen")]
    driver = create_driver() if moka_companies else None
    if driver is None:
        print("所有公司均为接口爬取，无需启动浏览器")
    try:
        results = []
        for company in companies:
            r = process_company(driver, company, is_monday)
            if r:
                results.append(r)
        if not is_monday and results:
            send_daily_digest(results)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
            print("\n浏览器已关闭")


if __name__ == "__main__":
    main()
