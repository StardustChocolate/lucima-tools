# 本地全量资源

此目录用于原样暂存外部资源库，不参与 LucimaTools 构建，也不提交到 Git。
仓库只跟踪本说明文件；不要为了 LucimaTools 调整这里的目录或文件名。

当前同步映射如下：

| 构建资源 | 全量资源来源 | 选择规则 |
|---|---|---|
| `assets/avatars/` | `团员/头像/<角色目录>/` | 每个角色的 `Icon_Head_S_<ID>.png`，输出为 `<ID>.png` |
| `assets/equip/` | `装备/图标/` | `backend/equip_ref.json` 引用的装备 ID，输出为 `<ID>.png` |
| `assets/sets/` | `装备/套装图标/` | `backend/tasks.py` 使用的套装 ID，输出为运行时图标名 |

在项目根执行同步：

```powershell
python tools/sync_assets.py
```

同步脚本会先完成全部映射检查，再更新项目根 `assets/`。缺少必需的装备图或
套装图时不会修改目标；名称表中存在但全量库没有的角色头像会给出警告并继续同步。
其他页面、脚本、数据和图片类别不会进入构建资源。
