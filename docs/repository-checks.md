# 仓库检查说明

本仓库的门禁由 `scripts/repo_checks.py` 统一承担，本地 pre-commit 钩子与 GitHub CI 调用同一入口，检查口径一致。全部为 Python 3.11+ 标准库实现，无第三方依赖。

## 首次使用

```text
python scripts/install_hooks.py
```

安装仓库局部钩子（只写本仓库的 `core.hooksPath`，不改全局 Git 配置；已存在不同的 hooksPath 时拒绝覆盖）。之后每次提交前自动运行暂存区检查。

## 检查内容

入口 `python scripts/repo_checks.py`，两个阶段，任一失败即非零退出：

1. 文件检查（`scripts/repo_guard.py` 与 `repo_checks.py` 内建规则）
   - 隐私扫描：明文密钥赋值（名字含 key/token/secret/password 等词段，值为 8 字符以上 ASCII 字面量），覆盖三种常见形态——无引号键+引号值（Python/env）、JSON 引号键、无引号 YAML·env 裸标量（无引号值须为含字母和数字的单个词元）；另有 token/私钥形态、个人绝对路径、文档署名。仅按完整占位格式放行：`xxxx-xxxx` 全 x 串、`<...>`、`${...}`、`{{ ... }}`、`your-...` 前缀及 changeme/example 等整词；值中间夹着占位词不豁免。环境变量引用和运行时拼接的合成样例天然不命中。
   - 本地产物拦截：`.dev-flow`、`local-private`、`runtime`、`logs` 等目录，`.bak/.log/.tmp/.swp/.pyc` 后缀，文件名含 handover/receipt/交接/回执，以及 `*.local.*` 配置覆盖。
   - 语法检查：受跟踪 `.py` 文件 `ast.parse`，不执行被扫描模块。
   - 基础格式：`.py`/`.yml` 行尾空白、`.py`/`.md`/`.yml` 末尾换行。`.md` 不查行尾空白（Markdown 双空格换行是合法语法）；`.bat` 规范形态为 CRLF，不做格式检查。
2. 测试执行：递归收集 `tests/` 下全部 `test_*.py`，按相对路径生成唯一模块名逐个加载执行。嵌套目录无 `__init__.py`、跨目录同名测试文件都会执行；零个测试文件、零个用例、任一加载失败或任一测试失败/报错均非零退出。
   - 适配器单一归属：定义模块级 `load_tests` 的文件视为套件适配器，先于直接发现执行；适配器导入过的收集文件归其所有，不再被直接发现重复执行。用真实适配器形态组合验证过每个源用例恰好执行一次。

## 全量与暂存区语义

- 全量模式（CI，默认）：检查 `git ls-files` 列出的全部跟踪文件，读工作区内容。
- `--staged` 模式（钩子）：只检查已暂存（`git diff --cached`，ACMR）文件，读暂存区内容（`git show :path`）。工作区已修复但暂存区仍含违规内容时会继续拦截，须重新 `git add`。

路径一律从项目根目录解析，可从任意当前目录调用。

## 边界（如实声明）

- 本地钩子可跳过（`git commit --no-verify`、取消 `core.hooksPath`），是便利措施不是安全边界；远程 CI 和受保护分支才是多人合并约束。
- CI 是保守扫描，不是完整秘密扫描；提交者仍须人工检查 diff。
- 人工审批（主控审核后才合并/关闭 Issue）是协作流程约定，目前没有对应的平台强制门禁；配置 PR 和 CI 并不能保证只有主控能点合并。
