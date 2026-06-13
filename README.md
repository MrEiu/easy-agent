# scRNA Copilot Generic Skill Orchestrator V4

这是通用 Skill Orchestrator 版本，不写死任何 scRNA skill 名称。

## 已实现

- Plan → Confirm → Run
- Preflight 检查
- run_manifest.json
- logs/events.jsonl 结构化日志
- output/<username>/<run_id>/ 隔离输出
- chatlog/<username>/ 聊天记录
- session summary: chatlog/<username>/<session>.summary.json
- Workspace Run 视角
- Skill validate
- 上传数据到 /data
- 上传 skill 到 /skill，zip 自动解压
- 错误分类
- skill 执行隔离工作目录和环境变量

## 目录

```text
skill/
data/
output/<username>/<run_id>/
chatlog/<username>/
agent.json
user.json
.ignore
```

## 运行

```bash
pip install python-multipart
python main.py
```

默认登录：

```text
account: admin
password: admin123
```

## Skill 约定

skill 可以是单文件：

```text
skill/my_tool.py
skill/my_tool.R
skill/my_tool.sh
```

也可以是目录：

```text
skill/my_tool/
  skill.json
  run.py
```

执行时后端会注入环境变量：

```text
SKILL_DIR
DATA_DIR
OUTPUT_DIR       # output/<username>/<run_id>
GLOBAL_OUTPUT_DIR
RUN_ID
USERNAME
RUN_MANIFEST
RUN_EVENTS
```
