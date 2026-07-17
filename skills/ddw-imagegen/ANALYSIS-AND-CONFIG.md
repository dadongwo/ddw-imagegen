# DDW Image Gen：分析与配置说明

## 1. 技能做什么

这是一个面向 Codex 的图像生成/编辑技能，入口定义在 `SKILL.md`，实际执行器是 `scripts/ddw_image_gen.py`。

主要能力：

- 自然语言生成、编辑、合成和批量生成图片。
- 通过异步 `/image-jobs` 提交任务并轮询结果。
- 本地校验输入与输出图片，原子写入结果文件。
- 保存可恢复任务状态；任务 token 存放在用户私有状态目录，不写入公开 ledger。
- 支持透明背景的本地色键预检与抠图辅助流程。
- 提供离线测试、模拟服务和 Codex Desktop 运行时检查。

## 2. 运行依赖

- Python 3.10+，建议使用 Codex bundled Python。
- 生成/编辑图片需要 Pillow；透明背景处理也依赖 Pillow。
- 网络访问 `DDW_IMAGE_BASE_URL` 指向的 DDW API。
- Codex 中需要把该目录安装为 skill，并通过 `$ddw-imagegen` 触发。

## 3. 配置方式

### Windows PowerShell

```powershell
$env:DDW_IMAGE_API_KEY = "你的 DDW API Key"
$env:DDW_IMAGE_BASE_URL = "https://api.ddwapi.dpdns.org"
```

也兼容旧变量名：

```powershell
$env:DDW_API_KEY = "你的 DDW API Key"
```

如果必须使用其他环境变量名，调用 CLI 时显式指定：

```powershell
python scripts/ddw_image_gen.py create --api-key-env API_KEY --prompt "..." --out "D:/output/image.png"
```

不要把 Key 写入 `SKILL.md`、源码、Git、压缩包或聊天记录。推荐使用当前终端会话环境变量；如需持久化，请使用 Windows 用户环境变量或密码管理器。

## 4. CLI 示例

生成：

```powershell
python scripts/ddw_image_gen.py create --prompt "一只戴红围巾的橘猫，电影感光线" --out "D:/output/cat.png"
```

编辑：

```powershell
python scripts/ddw_image_gen.py create --prompt "保留主体和构图，把背景改成雪山" --image "D:/input/source.png" --out "D:/output/edited.png"
```

离线检查：

```powershell
python scripts/ddw_image_gen.py create --dry-run --prompt "test"
python scripts/run_acceptance_tests.py --offline
```

## 5. 配置项概览

| 配置项 | 用途 |
|---|---|
| `DDW_IMAGE_API_KEY` | 首选 DDW API Key 环境变量 |
| `DDW_API_KEY` | 兼容旧配置的 Key 环境变量 |
| `DDW_IMAGE_BASE_URL` | API 基地址，默认 `https://api.ddwapi.dpdns.org` |
| `DDW_IMAGE_JOB_LEDGER` | 诊断时覆盖任务 ledger 路径，不建议常规使用 |
| `DDW_IMAGE_TOKEN_STORE` | 诊断时覆盖任务 token 私有存储路径，不建议常规使用 |
| `CODEX_HOME` | Codex 状态根目录；未设置时使用 `~/.codex` |

默认任务状态位于 `$CODEX_HOME/state/ddw-imagegen/`。这些状态可能包含任务恢复信息，不应随技能包转发。

## 6. 给分析者的重点

- 入口规则和安全边界：`SKILL.md`。
- HTTP、鉴权、异步任务和恢复逻辑：`scripts/ddw_image_gen.py`。
- 网络接口约定：`references/codex-network.md`。
- CLI 参数、状态 ledger 和高级诊断：`references/cli.md`。
- 提示词构造：`references/prompting.md` 与 `references/sample-prompts.md`。
- 回归约束：`references/review-and-test-notes.md` 与 `tests/`。
- Codex UI 元数据：`agents/openai.yaml`。

重点审查问题：

1. 是否会重复提交已进入异步状态的付费任务。
2. API Key、任务 token、错误 URL 中的 token 是否会进入日志或输出。
3. Base URL 校验是否阻止不安全的本地/内网目标。
4. 输出数量、图片格式和原子保存是否经过校验。
5. 透明背景流程是否在付费提交前完成本地预检。

## 7. 本包不包含什么

- 不包含真实 API Key、任务 token、Codex 私有状态目录或输出图片。
- 已排除 Python `__pycache__` 缓存。
- 包内测试中的 token 是测试夹具，不是生产凭据。
