# 协作平台 API 接入指南与接口说明

## 1. 如何在协作平台中获取和管理 API 凭据（`credential`）？

在调用协作平台固定产品 API 之前，你需要先在平台页面创建或查看绑定你本人账号的 API 凭据。

- **适用角色**：全体角色（`admin`、`member`、`guest` 均可管理本人名下的 API 凭据；`admin` 还可在空间视角审计凭据调用情况）。
- **前提条件**：已登录协作平台并进入目标工作空间（`workspace`）。
- **页面路径与按钮**：左侧导航栏 **「开发者与 API 中心 > 凭据管理」** → 右上角 **「+ 创建 API 凭据」** 按钮。
- **操作步骤**：
  1. 在左侧导航栏点击 **「开发者与 API 中心 > 凭据管理」**，查看当前账号名下已有的凭据列表（列表展示凭据名称、脱敏标识 `credential_id`、沙箱测试标识 `test_credential_id`、已勾选的权限范围 `scope`、有效期及状态）。
  2. 如需新建凭据，点击右上角 **「+ 创建 API 凭据」**，在弹窗中填写：
     - **凭据名称**（必填）：如“研发看板同步脚本”。
     - **接口权限范围（`scope`）**：
       - `profile:read`（默认勾选）：允许调用身份查询接口 `GET /sandbox/product/v1/me`。
       - `project:read`（按需勾选）：允许调用项目列表接口 `GET /sandbox/product/v1/projects` 与项目详情接口 `GET /sandbox/product/v1/projects/{id}`。
     - **有效期**（必填）：支持选择 `7 天`、`30 天`、`90 天` 或自定义到期时间。
  3. 点击 **「生成凭据」**。弹窗会一次性展示明文访问令牌（`credential_token`，示例占位格式如 `vm_live_<YOUR_TOKEN_PLACEHOLDER>`）以及对应的受控沙箱测试标识（`test_credential_id`，格式如 `tcred_01`）。请立即复制并妥善保存明文令牌；关闭弹窗后，平台仅存储其哈希值与脱敏标识，不再显示明文。
- **预期结果**：获得绑定当前租户（`tenant`）、当前工作空间（`workspace`）与当前调用用户身份的有效 API 凭据。
- **失败时的处理方法**：
  - **忘记已创建凭据的明文令牌**：平台不支持找回明文令牌。请在 **「凭据管理」** 页面点击原凭据右侧的 **「吊销」**，然后点击 **「+ 创建 API 凭据」** 生成新凭据；如果仅需在平台网页端调试接口，可直接在 **「开发者与 API 中心 > 沙箱调试台」** 页面选择列表中的 `test_credential_id` 发起测试。

## 2. 调用固定产品 API 时，认证方式、请求头与四重校验规则是什么？

### 2.1 协议、请求头与认证方式
所有固定产品 API 均采用标准 HTTP `GET` 方法，返回 UTF-8 编码的 JSON 格式数据：

- **外部客户端调用请求头**：
  - `Authorization: Bearer <YOUR_CREDENTIAL_TOKEN>`（必填，将 `<YOUR_CREDENTIAL_TOKEN>` 替换为你实际生成的有效凭据令牌）
  - `Accept: application/json`
- **平台「沙箱调试台」页面调用方式**：
  - 在左侧导航栏 **「开发者与 API 中心 > 沙箱调试台」** 页面中，无需手动粘贴明文令牌，直接在下拉框中选择本人名下的受控测试凭据标识（`test_credential_id`）与固定操作标识（`operation_id`），点击 **「发送沙箱请求」** 即可由平台后端加载对应凭据完成测试（注：Ordo 智能支持中心聊天窗仅提供接口规范讲解与报错分析，不直接代为运行沙箱请求）。

### 2.2 服务端四重校验顺序与核心原则

服务端收到每一次 API 请求时，严格按以下四步顺序执行校验，任一环节未通过即中止并返回对应错误：

| 校验顺序 | 校验环节 | 具体校验规则 | 未通过时的 HTTP 状态码与错误码 |
| :--- | :--- | :--- | :--- |
| **第 1 步** | **参数与请求结构校验** | 校验 HTTP 方法是否为 `GET`、路径是否正确、路径参数 `{id}` 格式是否合法、是否包含未定义或越界的查询参数 | HTTP `400`（`INVALID_ARGUMENT`） |
| **第 2 步** | **凭据有效性认证** | 校验 `Authorization` 请求头是否存在、凭据是否真实有效、是否已过期或已吊销，以及绑定的租户与工作空间是否正常 | HTTP `401`（`UNAUTHORIZED`） |
| **第 3 步** | **`scope` 与用户资源权限双重校验** | **条件 A（接口 `scope`）**：凭据是否勾选了当前接口所需的 `scope`（如 `project:read`）；<br>**条件 B（用户资源访问权）**：凭据绑定的调用用户本人是否对目标项目拥有访问权 | HTTP `403`（区分 `INSUFFICIENT_SCOPE`、`PROJECT_ACCESS_DENIED`、`SCOPE_AND_ACCESS_DENIED`） |
| **第 4 步** | **频控与累计配额校验** | 校验该凭据在当前 60 秒固定窗口内的请求次数（上限 **60 次/60 秒**），以及所属工作空间当日累计调用次数（上限 **5,000 次/自然日**，每日 **00:00 UTC+8** 自动重置） | HTTP `429`（区分 `RATE_LIMITED` 与 `QUOTA_EXCEEDED`） |

> **必须牢记的两条核心鉴权边界**：
> 1. **`project:read` 与调用用户的项目资源访问权是两个独立条件**：只有同时满足“凭据包含 `project:read`”和“调用用户本人有权访问该项目”，且凭据有效、配额充足，请求才能成功。
> 2. **工作空间管理员（`admin`）不能跳过凭据 `scope` 校验**：`admin` 在本工作空间管理范围内默认拥有全部公开项目与私有项目的资源访问权（满足条件 B），但如果 `admin` 使用的 API 凭据未勾选 `project:read`（不满足条件 A），调用项目接口依然会返回 HTTP `403`；同时，`admin` 严禁跨租户调用其他企业的项目。

## 3. 接口一：`GET /sandbox/product/v1/me`（查询当前调用身份）

- **接口用途与适用场景**：查询当前 API 凭据所绑定的用户身份、所属租户、工作空间及空间角色（`admin` / `member` / `guest`）。常用于接入初期的凭据连通性验证，以及排查 `401` 或身份绑定不符问题。
- **固定接口属性**：
  - **请求方法与路径**：`GET /sandbox/product/v1/me`
  - **沙箱操作标识（`operation_id`）**：`get_current_user`
  - **所需 `scope`**：`profile:read`（所有新建凭据默认具备，无需 `project:read`）
  - **路径参数与查询参数**：无（传入任何未定义查询参数将返回 HTTP `400`）
- **请求示例（使用占位符）**：
  ```http
  GET /sandbox/product/v1/me HTTP/1.1
  Host: api.workspace.example.com
  Authorization: Bearer <YOUR_CREDENTIAL_TOKEN>
  Accept: application/json
  ```
- **成功响应示例（HTTP `200 OK`）**：
  ```json
  {
    "status": "ok",
    "request_id": "req_20261016_001",
    "environment": "sandbox",
    "data": {
      "user_id": "usr_lin_08",
      "user_name": "林工",
      "tenant_id": "ten_xh_01",
      "workspace_id": "ws_xh_main",
      "role": "member",
      "credential_id": "cred_lin_dev01",
      "scopes": ["profile:read", "project:read"]
    }
  }
  ```
- **常见错误与检查步骤**：
  - 返回 HTTP `401`（`UNAUTHORIZED`）：检查 `Authorization` 头是否漏传 `Bearer ` 前缀，或在 **「开发者与 API 中心 > 凭据管理」** 检查该凭据是否已过期/已吊销。
  - 返回 HTTP `400`（`INVALID_ARGUMENT`）：检查请求 URL 末尾是否误拼接了未定义的查询参数（如 `?user_id=xxx`）。

## 4. 接口二：`GET /sandbox/product/v1/projects`（查询可见项目列表）

- **接口用途与适用场景**：分页获取当前调用用户在所属工作空间内**有权访问**的项目列表。
  - 当调用用户为 `admin` 时：返回本工作空间的全部公开项目（`workspace_public`）与全部私有项目（`private`）。
  - 当调用用户为 `member` 时：返回本工作空间的全部公开项目（`workspace_public`），以及本人创建或已获授权的私有项目（`private`）。
  - 当调用用户为 `guest` 时：仅返回本工作空间内已明确授权给该 `guest` 的项目。
- **固定接口属性**：
  - **请求方法与路径**：`GET /sandbox/product/v1/projects`
  - **沙箱操作标识（`operation_id`）**：`list_visible_projects`
  - **所需 `scope`**：必须包含 **`project:read`**
  - **查询参数（Query Parameters，均为可选）**：

| 参数名 | 类型 | 是否必填 | 约束与默认值 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `visibility` | `string` | 否 | 仅允许枚举值 `workspace_public` 或 `private` | 按项目可见性过滤当前用户有权访问的项目；传入其他值返回 `400` |
| `page` | `integer` | 否 | 正整数，`>= 1`，默认值为 `1` | 分页页码；传入 `0`、负数或非数字返回 `400` |
| `page_size` | `integer` | 否 | 正整数，取值范围 `1`～`50`，默认值为 `20` | 每页返回条数；超出 `50` 或小于 `1` 返回 `400` |

- **请求示例（使用占位符）**：
  ```http
  GET /sandbox/product/v1/projects?visibility=workspace_public&page=1&page_size=20 HTTP/1.1
  Host: api.workspace.example.com
  Authorization: Bearer <YOUR_CREDENTIAL_TOKEN>
  Accept: application/json
  ```
- **成功响应示例（HTTP `200 OK`）**：
  ```json
  {
    "status": "ok",
    "request_id": "req_20261016_002",
    "environment": "sandbox",
    "data": {
      "workspace_id": "ws_xh_main",
      "page": 1,
      "page_size": 20,
      "total": 1,
      "items": [
        {
          "project_id": "proj_roadmap_2026",
          "name": "2026 产品年度路线图",
          "visibility": "workspace_public",
          "my_permission": "edit",
          "owner_user_id": "usr_chen_01",
          "updated_at": "2026-10-15T09:30:00Z"
        }
      ]
    }
  }
  ```
- **常见错误与检查步骤**：
  - 返回 HTTP `403`（`INSUFFICIENT_SCOPE`）：当前凭据未勾选 `project:read`，需在 **「凭据管理」** 中勾选 `project:read`。
  - 返回结果为空列表 `items: []`（无报错）：若调用用户为 `guest` 且尚未被任何项目添加为协作者，属于正常权限过滤结果，需先获取具体项目的授权。

## 5. 接口三：`GET /sandbox/product/v1/projects/{id}`（查询单个项目详情）

- **接口用途与适用场景**：根据项目唯一标识 `{id}` 查询该项目的详细配置、负责人信息、当前用户的项目权限等级及项目内文档计数。
- **固定接口属性**：
  - **请求方法与路径**：`GET /sandbox/product/v1/projects/{id}`
  - **沙箱操作标识（`operation_id`）**：`get_project_detail`
  - **所需 `scope`**：必须包含 **`project:read`**，**且**凭据绑定的调用用户本人必须对该项目 `{id}` 拥有访问权限。
  - **路径参数与查询参数约束**：

| 参数名 | 参数位置 | 类型 | 是否必填 | 格式与校验约束 |
| :--- | :--- | :--- | :--- | :--- |
| `{id}` | Path（路径参数） | `string` | **必填** | 必须为以 `proj_` 开头的小写字母、数字或下划线组合，总长度 `8`～`32` 位（正则：`^proj_[a-z0-9_]{3,27}$`，与创建项目时的 `project_id` 规则一致）；传入中文项目名、大写字母或特殊字符将返回 HTTP `400` |
| 查询参数 | Query | — | 不支持 | 本接口不接收任何 Query 参数；附带未定义查询参数将返回 HTTP `400` |

- **请求示例（使用占位符）**：
  ```http
  GET /sandbox/product/v1/projects/proj_core_01 HTTP/1.1
  Host: api.workspace.example.com
  Authorization: Bearer <YOUR_CREDENTIAL_TOKEN>
  Accept: application/json
  ```
- **成功响应示例（HTTP `200 OK`）**：
  ```json
  {
    "status": "ok",
    "request_id": "req_20261016_003",
    "environment": "sandbox",
    "data": {
      "project_id": "proj_core_01",
      "workspace_id": "ws_xh_main",
      "name": "核心网关重构专项",
      "description": "负责 2026 Q4 网关鉴权与限流模块升级",
      "visibility": "private",
      "owner_user_id": "usr_zhao_02",
      "my_permission": "read",
      "document_count": 6,
      "member_count": 5,
      "updated_at": "2026-10-15T14:20:00Z"
    }
  }
  ```
- **常见错误与检查步骤**：
  - 返回 HTTP `400`（`INVALID_ARGUMENT`）：检查 `{id}` 是否误传了项目中文名（如 `/projects/核心网关重构专项`）或不符合 `^proj_[a-z0-9_]{3,27}$` 格式，应改为 `proj_core_01`。
  - 返回 HTTP `403`：请对照《06 API 错误排查手册》区分是缺 `project:read`（`INSUFFICIENT_SCOPE`）、缺项目访问权限（`PROJECT_ACCESS_DENIED`），还是两者同时缺失（`SCOPE_AND_ACCESS_DENIED`）。

## 6. 如何使用平台「沙箱调试台」进行测试与原身份复测？

- **适用角色**：全体角色（`admin`、`member`、`guest`，用户在页面仅能选择本人名下的受控测试凭据）。
- **页面路径与职责区分**：
  - **实际执行测试的入口**：左侧导航栏 **「开发者与 API 中心 > 沙箱调试台」** 页面（或你本地的 API 客户端工具）。
  - **Ordo 智能支持中心的职责边界**：右上角 **「帮助与支持 > Ordo 智能支持中心」** 仅负责为你讲解接口参数规范、分析你粘贴的脱敏报错信息并提供排查步骤指导，**不支持在聊天窗内直接传入 `test_credential_id` 代为运行沙箱请求或校验后台凭据**。
- **在「沙箱调试台」页面执行测试的操作步骤**：
  1. 在左侧导航栏进入 **「开发者与 API 中心 > 沙箱调试台」** 页面。
  2. 在 **「选择测试凭据（`test_credential_id`）」** 下拉框中，选择你要验证的本人凭据（如 `tcred_lin_01`，页面会自动关联你名下的对应凭据，无需手动粘贴明文 Token）。
  3. 在 **「选择接口操作（`operation_id`）」** 中选择目标接口：
     - `get_current_user`（对应 `GET /sandbox/product/v1/me`）
     - `list_visible_projects`（对应 `GET /sandbox/product/v1/projects`，可按需填写 `visibility`、`page`、`page_size`）
     - `get_project_detail`（对应 `GET /sandbox/product/v1/projects/{id}`，必填符合 `^proj_[a-z0-9_]{3,27}$` 的路径参数 `id`）
  4. 点击 **「发送沙箱请求」** 按钮，页面右侧结果面板会展示带有 `"environment": "sandbox"` 标识的响应状态码、响应头与完整 JSON 报文。
- **修复后的复测铁律**：
  1. **必须使用原调用身份及原凭据复测**：当你修复了凭据 `scope` 或完成了项目访问权限审批后，**必须在「沙箱调试台」或客户端中继续选用原先报错的那个用户身份及对应的原凭据重新测试**，严禁切换成 `admin` 的凭据代测来掩盖原账号的权限问题。
  2. **沙箱验证通过不自动代表外部生产环境故障已解决**：沙箱返回 `200 OK`（带 `sandbox` 标识）仅证明该凭据、权限配置与参数在平台受控沙箱中已调通；若你的外部业务脚本仍报错，还需检查外部脚本是否缓存了旧 Token、是否拼错了请求 URL，或是否因高频调用触发了 `429` 限流。

## 7. 示例：管理员与普通成员接入项目详情 API 的差异对比

> **示例说明（以下为假设情境，仅供演示接口校验规则，非当前用户真实状态）**：  
> **前提设定**：假设在“星海科技（`ws_xh_main`）”工作空间有一私有项目 `proj_secret_08`。工作空间管理员张总（`admin`）创建了凭据 `cred_admin_no_scope`（仅勾选 `profile:read`，未勾选 `project:read`）；正式成员小林（`member`，尚未加入 `proj_secret_08` 协作者名单）创建了凭据 `cred_lin_full`（已勾选 `profile:read` 和 `project:read`）。

1. **张总（`admin`）调用 `GET /sandbox/product/v1/projects/proj_secret_08` 为什么返回 `403`？**
   - 张总作为本空间 `admin`，在业务上天然拥有 `proj_secret_08` 的项目访问权；但 API 校验要求“凭据 `scope`”与“用户资源权限”同时满足。由于 `cred_admin_no_scope` 未勾选 `project:read`，请求在第 3 步校验被拦截，返回 `403 INSUFFICIENT_SCOPE`。
   - **解决与验证方式**：张总前往 **「开发者与 API 中心 > 凭据管理」** 页面为 `cred_admin_no_scope` 勾选 `project:read` 并保存，随后在 **「沙箱调试台」** 页面继续选用该凭据复测并观察结果（在凭据有效且配额可用时即可返回 `200 OK`）。
2. **小林（`member`）调用同一接口为什么也返回 `403`？**
   - 小林的凭据 `cred_lin_full` 已包含 `project:read`，但 `proj_secret_08` 为私有项目，小林不在该项目协作者列表中，因此返回 `403 PROJECT_ACCESS_DENIED`。
   - **解决与验证方式**：小林在 **「项目中心 > 申请访问项目」** 页面提交 `proj_secret_08` 的项目 `read` 权限申请，经张总在 **「空间管理 > 权限审批中心」**（或该项目负责人在项目内）批准为 `approved` 后，小林在 **「沙箱调试台」** 页面继续使用自己的原凭据 `cred_lin_full` 复测并观察结果。
