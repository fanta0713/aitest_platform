"""
禅道 API 客户端（v1 REST API: /api.php/v1）
"""
import httpx
from typing import Optional, Dict, Any, List
from app.core.config import get_settings


class ZentaoClient:
    """禅道API客户端"""

    def __init__(self, token: Optional[str] = None):
        self.settings = get_settings()
        self.base_url = self.settings.zentao_url.rstrip("/")
        self.token = token or self.settings.zentao_token

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Token"] = self.token
        return headers

    async def login(self, account: str, password: str) -> Optional[str]:
        """用账号密码换取Token，失败返回None"""
        url = f"{self.base_url}/api.php/v1/tokens"
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(
                    url, json={"account": account, "password": password}
                )
                if resp.status_code == 200 or resp.status_code == 201:
                    data = resp.json()
                    return data.get("token")
            except httpx.HTTPError:
                return None
        return None

    async def get(self, path: str, params: Optional[Dict] = None) -> Any:
        """GET请求"""
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def post(self, path: str, json: Optional[Dict] = None) -> Any:
        """POST请求（处理禅道空响应：builds/releases创建返回201/200但body为空）"""
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=json, headers=self._headers())
            resp.raise_for_status()
            if resp.status_code in (200, 201) and not resp.content:
                return {"result": "success"}
            try:
                return resp.json()
            except Exception:
                return {"result": "success"}

    # === 用户相关 ===
    async def get_users(self, limit: int = 200) -> List[Dict]:
        """获取全部用户列表"""
        result = await self.get("/api.php/v1/users", params={"limit": limit})
        return result.get("users", [])

    # === 项目相关 ===
    async def get_projects(self, limit: int = 100) -> List[Dict]:
        """获取项目列表"""
        result = await self.get("/api.php/v1/projects", params={"limit": limit})
        return result.get("projects", [])

    async def get_project(self, project_id: int) -> Dict:
        """获取单个项目详情（含begin/end日期）"""
        return await self.get(f"/api.php/v1/projects/{project_id}")

    # === 产品相关 ===
    async def get_products(self, limit: int = 200) -> List[Dict]:
        """获取产品列表"""
        result = await self.get("/api.php/v1/products", params={"limit": limit})
        return result.get("products", [])

    async def get_testtasks(self, product_id: int) -> List[Dict]:
        """获取产品的测试单列表"""
        result = await self.get("/api.php/v1/testtasks", params={"product": product_id, "limit": 100})
        return result.get("testtasks", [])

    async def get_testcases(self, product_id: int, limit: int = 500) -> List[Dict]:
        """获取产品的测试用例列表"""
        result = await self.get("/api.php/v1/testcases", params={"product": product_id, "limit": limit})
        return result.get("testcases", [])

    async def create_testcase(self, product_id: int, title: str, steps: List[Dict],
                              pri: str = "2", case_type: str = "feature",
                              stage: str = "feature", precondition: str = "") -> Dict:
        """创建测试用例"""
        body = {
            "product": product_id, "title": title, "pri": pri,
            "type": case_type, "stage": stage, "precondition": precondition,
            "steps": steps,
        }
        return await self.post(f"/api.php/v1/testcases?product={product_id}", json=body)

    async def create_testtask(self, project_id: int, execution_id: int, product_id: int,
                              name: str, build_id: int, begin: str, end: str,
                              owner: str = "admin", testtask_type: str = "system") -> Dict:
        """创建测试单。project必须通过URL query传递"""
        url = f"/api.php/v1/testtasks?project={project_id}"
        body = {
            "execution": execution_id, "product": product_id, "name": name,
            "build": build_id, "begin": begin, "end": end,
            "owner": owner, "type": testtask_type,
        }
        return await self.post(url, json=body)

    async def get_executions(self, project_id: int) -> List[Dict]:
        """获取项目的执行列表。禅道API的project过滤不可靠，客户端二次过滤"""
        result = await self.get("/api.php/v1/executions", params={"project": project_id, "limit": 50})
        execs = result.get("executions", [])
        # 二次过滤：只保留 project 或 parent 字段匹配的执行
        return [e for e in execs if int(e.get("project") or e.get("parent") or 0) == project_id]

    async def get_execution_for_product(self, project_id: int, product_id: int) -> Optional[int]:
        """获取项目下关联了指定产品的执行ID（需要拉执行详情查products字段）"""
        execs = await self.get_executions(project_id)
        for e in execs:
            try:
                detail = await self.get(f"/api.php/v1/executions/{e.get('id')}")
                products = detail.get("products", [])
                if any(int(p.get("id", 0)) == product_id for p in products):
                    return e.get("id")
            except Exception:
                continue
        return None

    async def get_builds(self, project_id: int) -> List[Dict]:
        """获取项目的构建列表"""
        result = await self.get("/api.php/v1/builds", params={"project": project_id, "limit": 50})
        return result.get("builds", [])

    async def create_product(self, name: str, code: str = "", product_type: str = "normal", desc: str = "") -> Dict:
        """创建产品，返回禅道产品对象"""
        return await self.post("/api.php/v1/products", json={
            "name": name, "code": code, "type": product_type, "desc": desc
        })

    async def create_project(self, name: str, products: List[int], code: str = "",
                             model: str = "waterfall", begin: str = None,
                             end: str = None, days: int = 0, desc: str = "") -> Dict:
        """创建项目（需关联至少一个产品），返回禅道项目对象"""
        body = {
            "name": name, "code": code, "model": model,
            "products": products, "days": days, "desc": desc,
        }
        if begin:
            body["begin"] = begin
        if end:
            body["end"] = end
        return await self.post("/api.php/v1/projects", json=body)

    async def create_execution(self, project_id: int, name: str, begin: str,
                               end: str, products: List[int] = None, code: str = "",
                               desc: str = "") -> Dict:
        """为项目创建执行，返回执行对象。project必须通过URL query传递"""
        url = f"/api.php/v1/executions?project={project_id}"
        body = {"name": name, "begin": begin, "end": end, "desc": desc}
        if code:
            body["code"] = code
        if products:
            body["products"] = products
        return await self.post(url, json=body)

    async def create_build(self, project_id: int, execution_id: int, product_id: int,
                           name: str, builder: str, date_str: str,
                           build_version: str = "", desc: str = "") -> Dict:
        """为项目创建构建。project必须通过URL query传递，其余字段在body"""
        url = f"/api.php/v1/builds?project={project_id}"
        body = {
            "execution": execution_id, "product": product_id,
            "name": name, "builder": builder, "date": date_str,
            "build": build_version, "desc": desc,
        }
        return await self.post(url, json=body)

    async def create_project_release(self, project_id: int, name: str, product_id: int,
                                     date_str: str, build_id: int = 0, desc: str = "") -> Dict:
        """创建项目发布（POST /api.php/v1/projects/<pid>/releases）"""
        url = f"/api.php/v1/projects/{project_id}/releases"
        body = {
            "name": name, "product": product_id, "build": build_id,
            "date": date_str, "desc": desc,
        }
        return await self.post(url, json=body)

    async def create_product_release(self, product_id: int, name: str, date_str: str,
                                     build_id: int = 0, desc: str = "") -> bool:
        """产品发布通过projectreleases REST API创建（需项目关联）。
        此方法保留但当前未使用——产品发布统一在项目创建时生成。"""
        return False

    # === Bug相关 ===
    async def create_bug(self, bug_data: Dict) -> Dict:
        return await self.post("/api.php/v1/bugs", json=bug_data)


def get_zentao_client() -> ZentaoClient:
    return ZentaoClient()
