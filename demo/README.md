# 完全合成的 CALI 演示

全部输入由数学公式从零生成，不读取真实测量、车辆标识、A2L/HEX、供应商资料或内部知识库。数值及阈值仅用于软件演示，不具有工程验证意义。

安装项目及开发依赖后，从仓库根目录运行：

```powershell
python -m pip install -e ".[dev]"
python demo/run_demo.py
```

脚本只写入新的 `.demo-workspace/`，并拒绝覆盖已有内容。它强制禁用两个 LLM provider，通过真实 FastAPI 接口完成：创建合成 Wiki 草稿、导入合成 A2L 与 9 个 MF4 文件、创建并审核一条**虚构演示规则**、逐文件执行评估，并将结果写入 `.demo-workspace/demo_result.json`。默认不批准规则，结果为 9 个 OBSERVATION，但必须准确定位唯一异常；不会发布 Wiki。

唯一异常来自 `SYNTHETIC_N20_RL50.mf4` 在 20–24 秒人为注入的 lambda 偏移。若你确认这条虚构规则仅用于独立演示，可运行 `python demo/run_demo.py --workspace .demo-workspace-approved --approve-demo-rule`；此时按现有审批接口记录规则批准，预期为 8 个 PASS、1 个 FAIL。任何结果或异常时间窗偏离预期都会报错。另有 OBFCM 合成 JSON，预期误差为 2.0%。

仅生成输入而不写入数据库：

```powershell
python demo/generate_demo.py --output-dir demo/generated
```

生成器提供文件清单、SHA-256、预期结果与说明文档。信号值确定可复现；MF4 保存历史中的写入时间可能变化。

如需在现有 UI 中查看演示，可在完成前端构建后，设置 `CALI_WORKSPACE_ROOT` 为本演示工作区的绝对路径，再运行 `python -m backend.main`。同时设置 `CALI_LLM_PROVIDER=disabled` 与 `CALI_WIKI_AGENT_PROVIDER=disabled`。不要把变量指向内部工作区。重复演示请用 `--workspace` 指定另一个空目录。
