# 有边界的 GitHub 上游同步

仅适用于 `Jerrychenshen/danmu_api_fork` 的 `main`。每天 **20:17 UTC** 和手动运行；没有 push 触发器。单一并发组排队执行，15 分钟超时。只使用 GitHub 自带令牌及官方 checkout/setup-node action，固定到经 GitHub 官方 release/tag API 核验的提交：

- checkout v7.0.1：`3d3c42e5aac5ba805825da76410c181273ba90b1`
- setup-node v7.1.0：`949feb2413d6458794dcd2491c4babbbce0c15c1`

两者 action.yml 使用 Node 24；应用测试也使用 Node 24。版本核验日期：2026-10-22。

## 同步边界

从当前远程 main 检出完整历史；工作目录必须完全干净，HEAD 必须等于检出的 origin/main。只从 `https://github.com/huangxd-/danmu_api.git` 的 main 获取上游；状态中的上次 SHA 必须仍是新上游的祖先。源文件按 Git 对象、模式和路径精确比较，包含删除和可执行位；拒绝子模块。若当前业务源树偏离上次上游，立即停止，交给人工合并。

`.github/` 整体以及 `.pr_agent.toml` 保持本地版本，包括原有删除状态。新上游的工作流不会导入。正常同步后的其他文件精确等于新上游。上游 SHA 写入 `.github/upstream-sync.json` 并保留在提交消息及链接中；保留源文件中的署名、许可证。

上游修改任意位置的 `vercel.json` / `vercel.ts` / `vercel.js`、`.vercelignore`、`.vercel/`、`.nvmrc`、`.node-version`、`.npmrc`、`Dockerfile` 时停止。package.json 的 engines、type、main、packageManager、os、cpu、volta 或 start/build/vercel-build/test 命令变化也停止。依赖版本变化允许进入 npm 测试。这些是明确的静态迁移检查，无法证明任意业务代码不存在语义上的运行时迁移；测试结果仍需结合真实 Vercel 构建判断。

脚本用独立临时 Git 索引构造候选树，不修改检出目录或使用广泛 git add。存在源变更时，在 runner 临时目录解包候选树，然后执行：

```sh
npm install --no-audit --no-fund
npm test
```

没有写入令牌传入 npm 步骤。依赖安装和测试产生的文件不会进入候选提交。只有测试成功后才以 `git commit-tree` 创建提交，提交前重新计算并核验整个计划。若上游只修改受保护文件，更新状态 SHA，保留本地控制文件并跳过 npm。

作者和提交者均显式设置为 `Jerrychenshen <37981711+Jerrychenshen@users.noreply.github.com>`，说明自动化代表仓库所有者执行，不修改全局 Git 配置，不伪造签名。

## 发布与验证（由维护者另行执行）

1. 审查并发布 `.github/` 变更，作者和提交者使用上述所有者身份。
2. 确认仓库允许 Actions 的 contents 写权限，main 的分支规则允许此自动化；禁止通过修改保护规则绕过拒绝。
3. 手动运行一次。默认 `verify_deploy=false`：无上游更新时，没有安装、构建或推送。
4. 如需验证 Actions 推送是否触发 Vercel，可明确将 `verify_deploy=true`。无上游更新时仅创建一个空的所有者提交，业务源树和状态都不变；每次显式运行都可能产生一个空提交。若存在上游更新，只执行正常更新，不再创建空提交。
5. 查看 Actions summary 的模式、上游 SHA 和新提交，再到现有 Vercel 项目检查关联提交、作者、构建和项目生产域名的实际部署。summary 中的成功只说明 Actions 步骤成功，不能证明部署成功。

写入令牌只在最后的 push 步骤作为环境变量提供；checkout 不持久化凭据。push 步骤通过进程级 http.extraheader 使用令牌，不落入命令行参数、输出或 `.git/config`，禁用 Git hooks。先读取远程 main 并与计划基准精确比较，再普通非强制 push。出现并发前进时停止；没有重试覆盖、远程 reset 或 force push。读取检查与 push 之间仍存在短暂竞态；普通 push 会拒绝不快进的并发提交，不能提供针对管理员强制回退/删分支的原子租约。不要在运行期间重写 main。

`GITHUB_TOKEN` 触发第三方 Vercel Git App 的行为需要实际端到端验证。若没有触发构建，记录阻塞并停止；不要增加 PAT、Vercel token、deploy hook、额外项目或更改仓库可见性。现有 Vercel 配置、域名和环境变量不由此工作流修改。仓库规则或 GitHub 调度延迟也可能阻止/延迟运行，cron 不承诺准点。

## 本地离线验证

只依赖 Python 标准库和已安装的 Git，不需要 npm、全局安装或任何账户凭据：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v
```

测试全部在本机临时仓库中运行，不访问远程、不推送。覆盖无更新幂等、源文件增改删和模式、控制文件保护、自定义变更拒绝、上游历史改写、脏目录、运行配置迁移、并发/错误基准、上下文限制、计划篡改、提交身份、显式空提交及状态推进。

## Fork 维护

上游固定为 `https://github.com/huangxd-/danmu_api.git` 的 `main`。优先使用本工作流同步；GitHub 原生手动 Sync fork 可能重新引入上游工作流或造成控制文件冲突，操作前必须人工审查。
