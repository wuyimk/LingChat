#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 LingChat 打上「对话中换装」补丁（幂等，可重复执行）。

用法（在仓库根目录）：
    python scripts/apply-clothes-patch.py

做的事（4 处，每处都带锚点校验，锚点对不上会明确报错退出）：
  1. src-tauri/src/ai_service/tools/character.rs  追加 CharacterClothes 工具
  2. src-tauri/src/ai_service/tools/mod.rs        导入 + 注册
  3. src-tauri/src/ai_service/tools/settings.rs   TOOL_GROUPS 的 character 组里加工具名
  4. src/api/tauri-events.ts                      前端监听 character:clothes

为什么用字符串锚点而不是 git patch：锚点不依赖行号，上游小改动也能命中；
命中不了会立刻失败，不会留下半成品。
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 注入的代码

RUST_STORE_IMPORT = "use tauri_plugin_store::StoreExt;"

RUST_TOOL = r'''
/// character_clothes：切换指定角色当前穿着的服装（立绘）。
///
/// 语义与 UI 用的 `select_clothes`（`src-tauri/src/api/character.rs`）保持一致：
/// 1. 持久化到 store（`last_clothes_key(role_id)`），下次启动沿用；
/// 2. 写入 role_manager 的服装覆盖值；
/// 3. 走 `GameStatus::on_character_change_clothes`（去重 + 追加「旁白」台词）；
/// 4. 额外 emit `character:clothes` 通知前端刷新 `role.clothesName` ——
///    `select_clothes` 命令本身不 emit，UI 调用时是前端自己写回 store；
///    工具调用没有 UI 参与，必须靠这个事件驱动立绘刷新。
pub struct CharacterClothes;

#[async_trait]
impl Tool for CharacterClothes {
    fn definition(&self) -> ToolDefinition {
        ToolDefinition::new(
            "character_clothes",
            "切换指定角色当前穿着的服装（立绘）。只能换成该角色已配置的服装名。",
            json!({
                "type": "object",
                "properties": {
                    "role_id": {"type": "integer", "description": "角色 ID；不确定时先调用 character_list"},
                    "clothes_name": {"type": "string", "description": "服装名，取自角色设置的 clothes[].name，例如 默认 / 泳装 / 睡衣"}
                },
                "required": ["role_id", "clothes_name"],
                "additionalProperties": false
            }),
        )
    }

    async fn execute(
        &self,
        context: &ToolContext,
        arguments: Value,
    ) -> Result<ToolResult, ToolError> {
        let Some(obj) = arguments.as_object() else {
            return Err(ToolError::InvalidArguments(
                "character_clothes 参数必须是 JSON object".into(),
            ));
        };

        let raw_role_id = obj
            .get("role_id")
            .and_then(Value::as_i64)
            .ok_or_else(|| {
                ToolError::InvalidArguments("character_clothes 需要整数 role_id".into())
            })?;
        let role_id = i32::try_from(raw_role_id).map_err(|_| {
            ToolError::InvalidArguments("character_clothes 的 role_id 超出 i32 范围".into())
        })?;

        let clothes_name = obj
            .get("clothes_name")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                ToolError::InvalidArguments("character_clothes 需要非空 clothes_name".into())
            })?
            .to_string();
        // UI 侧 "default" 与「默认」等价，统一成「默认」
        let clothes_name = if clothes_name == "default" {
            "默认".to_string()
        } else {
            clothes_name
        };

        let app = context.require_app()?;
        let state = app.state::<AppState>();

        let gs_handle = game_status_handle(&app).await;
        let mut gs = gs_handle.lock().await;

        // on_character_change_clothes 内部是 get_loaded_mut，角色没加载会直接报错。
        gs.get_role(&state.db, role_id)
            .await
            .map_err(|e| ToolError::Execution(format!("加载角色 {role_id} 失败: {e}")))?;

        // 校验服装名：只允许角色设置里真实存在的名字，避免换出一张空白立绘。
        // 用 serde_json 中转，避免依赖 CharacterSettings.clothes 的具体类型。
        let (ai_name, valid_names) = {
            let loaded = gs
                .role_manager
                .get_loaded(role_id)
                .ok_or_else(|| ToolError::Execution(format!("角色 {role_id} 加载后不可用")))?;
            let names: Vec<String> = serde_json::to_value(&loaded.settings.clothes)
                .ok()
                .and_then(|v| v.as_array().cloned())
                .map(|arr| {
                    arr.iter()
                        .filter_map(|it| {
                            it.get("name").and_then(Value::as_str).map(str::to_string)
                        })
                        .collect()
                })
                .unwrap_or_default();
            (loaded.settings.ai_name.clone(), names)
        };

        if !valid_names.is_empty()
            && clothes_name != "默认"
            && !valid_names.iter().any(|n| n == &clothes_name)
        {
            return Err(ToolError::Execution(format!(
                "角色「{ai_name}」没有服装「{clothes_name}」，可选: {}",
                valid_names.join(", ")
            )));
        }

        // 1) 持久化服装选择（与 select_clothes 一致）
        if let Ok(store) = app.store(crate::config::STORE_FILE) {
            store.set(
                crate::config::session::last_clothes_key(role_id),
                Value::String(clothes_name.clone()),
            );
            let _ = store.save();
        }

        // 2)
