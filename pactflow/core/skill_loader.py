import os
import re
import json
import time
from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from functools import lru_cache

from .config import SKILLS_DIR
from .logger import audit_logger
from .tools.sandbox_tools import execute_office_shell


class DynamicSkillInput(BaseModel):
    mode: str = Field(
        description="可选 'manifest'、'help' 或 'run'。manifest 查看轻量能力卡，help 查看完整说明书。"
    )
    command: Optional[str] = Field(
        default="",
        description="仅在 mode='run' 时需要。你要执行的完整命令，保留 {baseDir} 占位符。"
    )


class SkillDiscoveryInput(BaseModel):
    query: str = Field(description="用户目标或想完成的事情，用自然语言描述")
    top_k: int = Field(default=5, ge=1, le=10, description="最多返回多少个候选 Skill")
    max_risk: Optional[Literal["low", "medium", "high", "critical"]] = Field(
        default=None,
        description="可选的最高风险级别过滤，不代表授予执行权限",
    )


class LazySkillLoader:
    """
    渐进式技能加载器 + 缓存机制
    
    特性：
    1. 启动时只扫描元数据，不加载完整内容
    2. 首次调用技能时才加载完整内容并缓存
    3. 支持热更新（修改技能文件后自动重新加载）
    4. LRU缓存策略，自动清理不常用的技能
    5. 基于元数据的轻量检索，不把检索结果当作执行授权
    """
    
    def __init__(self, cache_size: int = 50):
        self._skill_registry: Optional[List[Dict[str, Any]]] = None
        self._cache_size = cache_size
        self._last_scan_time = 0
        self._scan_interval = 60  # 缓存元数据扫描结果60秒
    
    @lru_cache(maxsize=50)
    def _load_skill_content(self, md_path: str, mtime: float) -> str:
        """
        加载技能完整内容（带缓存）
        
        Args:
            md_path: 技能文件路径
            mtime: 文件修改时间（用于缓存失效检测）
        
        Returns:
            技能的完整 Markdown 内容
        """
        with open(md_path, "r", encoding="utf-8") as f:
            return f.read()
    
    def _scan_skills(self, force_rescan: bool = False) -> List[Dict[str, Any]]:
        """
        扫描技能目录，只提取元数据（轻量级操作）
        
        Args:
            force_rescan: 是否强制重新扫描（忽略缓存）
        
        Returns:
            技能元数据列表
        """
        current_time = time.time()
        
        # 缓存检查：如果最近扫描过且不强制刷新，直接返回缓存
        if (not force_rescan and 
            self._skill_registry is not None and 
            current_time - self._last_scan_time < self._scan_interval):
            return self._skill_registry
        
        skills = []
        
        if not os.path.exists(SKILLS_DIR):
            self._skill_registry = []
            self._last_scan_time = current_time
            return []
        
        for item in os.listdir(SKILLS_DIR):
            folder_path = os.path.join(SKILLS_DIR, item)
            if not os.path.isdir(folder_path):
                continue
            
            md_path = os.path.join(folder_path, "SKILL.md")
            if not os.path.exists(md_path):
                md_path = os.path.join(folder_path, "README.md")
            
            if not os.path.exists(md_path):
                continue
            
            try:
                # 只读取前几行（name, description）
                metadata = self._extract_metadata(md_path)
                
                if metadata:
                    skills.append({
                        "folder": item,
                        "md_path": md_path,
                        "mtime": os.path.getmtime(md_path),
                        **metadata
                    })
            except Exception as e:
                print(f" [警告] 扫描技能 {item} 失败: {e}")
        
        self._skill_registry = skills
        self._last_scan_time = current_time
        
        if skills:
            print(f" [OK] 扫描到 {len(skills)} 个技能（懒加载模式）")
        
        return skills
    
    @staticmethod
    def _parse_metadata_value(value: str) -> Any:
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed]
            except json.JSONDecodeError:
                value = value[1:-1]
        if "," in value:
            return [item.strip() for item in value.split(",") if item.strip()]
        return value.strip('"\'')

    def _extract_metadata(self, md_path: str) -> Optional[Dict[str, Any]]:
        """
        从技能文件中提取元数据（只读取必要的部分）
        
        Args:
            md_path: 技能文件路径
        
        Returns:
            包含 name 和 description 的字典
        """
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                # 只读取前 50 行（通常元数据在文件开头）
                lines = []
                for i, line in enumerate(f):
                    if i >= 50:
                        break
                    lines.append(line)
                
                content = "\n".join(lines)
            
            def field_match(field: str) -> Optional[str]:
                match = re.search(rf"^{re.escape(field)}:\s*(.+)$", content, re.MULTILINE)
                return match.group(1).strip() if match else None

            raw_name = field_match("name") or os.path.basename(os.path.dirname(md_path))
            tool_name = re.sub(r'[^a-zA-Z0-9_-]', '_', raw_name)

            raw_desc = field_match("description") or f"提供 {raw_name} 相关功能"
            if (raw_desc.startswith('"') and raw_desc.endswith('"')) or (raw_desc.startswith("'") and raw_desc.endswith("'")):
                raw_desc = raw_desc[1:-1]

            risk_level = str(field_match("risk_level") or "medium").lower()
            if risk_level not in {"low", "medium", "high", "critical"}:
                risk_level = "medium"
            trust_level = str(field_match("trust_level") or "external").lower()
            if trust_level not in {"trusted", "verified", "external", "untrusted"}:
                trust_level = "external"

            tags = self._parse_metadata_value(field_match("tags") or "")
            required_tools = self._parse_metadata_value(field_match("required_tools") or "")
            examples = self._parse_metadata_value(field_match("examples") or "")
            if isinstance(tags, str):
                tags = [tags] if tags else []
            if isinstance(required_tools, str):
                required_tools = [required_tools] if required_tools else []
            if isinstance(examples, str):
                examples = [examples] if examples else []
            
            return {
                "raw_name": raw_name,
                "name": tool_name,
                "description": raw_desc,
                "risk_level": risk_level,
                "trust_level": trust_level,
                "tags": tags,
                "required_tools": required_tools,
                "examples": examples,
            }
        except Exception as e:
            print(f" [警告] 提取元数据失败 {md_path}: {e}")
            return None

    @staticmethod
    def _tokens(value: str) -> list[str]:
        return [
            token
            for token in re.findall(r"[a-zA-Z0-9_-]+|[\u4e00-\u9fff]+", value.lower())
            if token
        ]

    @staticmethod
    def _risk_rank(risk_level: str) -> int:
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(risk_level, 1)

    @staticmethod
    def skill_manifest(skill_info: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": skill_info["name"],
            "raw_name": skill_info["raw_name"],
            "description": skill_info["description"],
            "risk_level": skill_info.get("risk_level", "medium"),
            "trust_level": skill_info.get("trust_level", "external"),
            "tags": list(skill_info.get("tags", [])),
            "required_tools": list(skill_info.get("required_tools", [])),
            "examples": list(skill_info.get("examples", [])),
            "requires_full_help": skill_info.get("risk_level", "medium") in {"high", "critical"},
        }

    def retrieve_skill_manifests(
        self,
        query: str,
        *,
        top_k: int = 5,
        max_risk: str | None = None,
        force_rescan: bool = False,
    ) -> List[Dict[str, Any]]:
        """Retrieve candidate skills from lightweight metadata only.

        Retrieval narrows the candidate set; it never grants execution permission.
        ContractToolNode and the skill risk gate remain the final enforcement points.
        """
        query_text = str(query or "").strip().lower()
        query_tokens = set(self._tokens(query_text))
        max_rank = self._risk_rank(max_risk) if max_risk else None
        ranked: list[tuple[float, Dict[str, Any]]] = []

        for skill in self._scan_skills(force_rescan=force_rescan):
            if max_rank is not None and self._risk_rank(skill["risk_level"]) > max_rank:
                continue
            searchable = " ".join([
                skill["raw_name"],
                skill["description"],
                " ".join(skill.get("tags", [])),
                " ".join(skill.get("required_tools", [])),
                " ".join(skill.get("examples", [])),
            ]).lower()
            searchable_tokens = set(self._tokens(searchable))
            overlap = len(query_tokens & searchable_tokens)
            score = float(overlap)
            if query_text and query_text in searchable:
                score += 2.0
            if query_text and query_text in skill["description"].lower():
                score += 1.0
            if score > 0 or not query_text:
                ranked.append((score, self.skill_manifest(skill)))

        ranked.sort(key=lambda item: (-item[0], item[1]["name"]))
        bounded_top_k = max(1, min(int(top_k), 10))
        return [manifest for _, manifest in ranked[:bounded_top_k]]
    
    def _create_lazy_tool(self, skill_info: Dict[str, Any]) -> StructuredTool:
        """
        创建懒加载工具对象
        
        Args:
            skill_info: 技能元数据
        
        Returns:
            LangChain 工具对象
        """
        def lazy_runner(mode: str, command: str = "") -> str:
            """懒加载执行器：首次调用时才加载完整内容"""
            if mode == "manifest":
                _viewed_skill_manifest.add(skill_info["name"])
                audit_logger.log_event(
                    thread_id="local_geek_master",
                    event="skill_manifest_viewed",
                    skill=skill_info["name"],
                    folder=skill_info["folder"],
                    risk_level=skill_info.get("risk_level", "medium"),
                )
                return json.dumps(
                    {
                        "stage": "manifest",
                        **self.skill_manifest(skill_info),
                        "next_step": "高风险 Skill 需要 mode='help'，其他 Skill 可在契约允许后 mode='run'。",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            elif mode == "help":
                # 懒加载：首次调用时才读取完整内容
                skill_content = self._load_skill_content(
                    skill_info["md_path"], 
                    skill_info["mtime"]
                )
                _viewed_skill_manifest.add(skill_info["name"])
                _viewed_skill_help.add(skill_info["name"])
                audit_logger.log_event(
                    thread_id="local_geek_master",
                    event="skill_help_viewed",
                    skill=skill_info["name"],
                    folder=skill_info["folder"],
                )
                
                return (
                    f"========== 【{skill_info['raw_name']} 完整说明书】 ==========\n"
                    f"{skill_content[:3000]}\n"
                    f"====================================\n"
                    f"提示：请根据以上说明，如果觉得能解决问题，就将 mode 设为 'run'，"
                    f"并将拼装好的执行命令填入 command 重新调用。"
                )
            elif mode == "run":
                if not command:
                    return "错误：在 'run' 模式下，必须提供 command 参数！"
                risk_level = skill_info.get("risk_level", "medium")
                trust_level = skill_info.get("trust_level", "external")
                manifest_seen = skill_info["name"] in _viewed_skill_manifest
                help_seen = skill_info["name"] in _viewed_skill_help
                auto_allowed = risk_level == "low" and trust_level in {"trusted", "verified"}
                preview_allowed = (
                    (risk_level == "medium" and (manifest_seen or help_seen))
                    or (risk_level in {"high", "critical"} and help_seen)
                    or (risk_level == "low" and (auto_allowed or manifest_seen or help_seen))
                )
                if not preview_allowed:
                    audit_logger.log_event(
                        thread_id="local_geek_master",
                        event="contract_violation",
                        decision="deny",
                        tool=skill_info["name"],
                        clause="skill.preview_required",
                        reason="动态技能必须先查看 Manifest；高风险技能必须先 help",
                    )
                    return (
                        "契约拒绝：动态技能必须先 help 再 run。"
                        "当前 Skill 需要先使用 mode='manifest' 预览；高风险 Skill 还必须完整 help。"
                    )

                audit_logger.log_event(
                    thread_id="local_geek_master",
                    event="skill_run_requested",
                    skill=skill_info["name"],
                    risk_level=risk_level,
                    preview="auto" if auto_allowed else ("help" if help_seen else "manifest"),
                )
                
                actual_cmd = command.replace("{baseDir}", f"skills/{skill_info['folder']}")
                return execute_office_shell.invoke({"command": actual_cmd})
            else:
                return "错误：mode 参数只能是 'manifest'、'help' 或 'run'。"
        
        mini_description = (
            f"{skill_info['description']}\n\n"
            f"风险级别：{skill_info.get('risk_level', 'medium')}；可信级别：{skill_info.get('trust_level', 'external')}。"
            f"可先使用 `mode='manifest'` 查看轻量能力卡；高风险技能必须 `mode='help'` 后才能 `mode='run'`。"
        )
        
        return StructuredTool.from_function(
            func=lazy_runner,
            name=skill_info["name"],
            description=mini_description,
            args_schema=DynamicSkillInput,
            metadata={
                "capability": "execute",
                "resource_arg": "command",
                "help_resource": f"skills/{skill_info['folder']}/SKILL.md",
                "skill_risk_level": skill_info.get("risk_level", "medium"),
                "skill_trust_level": skill_info.get("trust_level", "external"),
            },
        )
    
    def get_all_tools(self, force_rescan: bool = False) -> List[StructuredTool]:
        """
        获取所有工具（懒加载占位符）
        
        Args:
            force_rescan: 是否强制重新扫描技能目录
        
        Returns:
            工具对象列表
        """
        skill_infos = self._scan_skills(force_rescan=force_rescan)
        
        tools = []
        for skill_info in skill_infos:
            tools.append(self._create_lazy_tool(skill_info))
        
        return tools
    
    def get_tool_count(self) -> int:
        """获取技能数量（不触发加载）"""
        return len(self._scan_skills())
    
    def clear_cache(self):
        """清除所有缓存"""
        self._load_skill_content.cache_clear()
        self._skill_registry = None
        _viewed_skill_manifest.clear()
        _viewed_skill_help.clear()
        print(f" [OK] 技能缓存已清除")


# 当前进程内记录 Skill 的预览状态。高风险仍要求完整 help，保持 help -> run 兼容边界。
_viewed_skill_manifest: set[str] = set()
_viewed_skill_help: set[str] = set()

# 全局懒加载器实例
_lazy_loader = LazySkillLoader(cache_size=50)


def get_skill_discovery_tool() -> StructuredTool:
    """Expose metadata-only Skill discovery as a safe Agent capability."""
    def discover_skills(query: str, top_k: int = 5, max_risk: str | None = None) -> str:
        results = _lazy_loader.retrieve_skill_manifests(
            query,
            top_k=top_k,
            max_risk=max_risk,
        )
        audit_logger.log_event(
            thread_id="local_geek_master",
            event="skill_discovery",
            query=query[:200],
            result_count=len(results),
        )
        return json.dumps(
            {
                "query": query,
                "results": results,
                "note": "检索结果只用于缩小候选范围，不代表已获得执行权限。最终权限由任务契约和工具守卫决定。",
            },
            ensure_ascii=False,
            indent=2,
        )

    return StructuredTool.from_function(
        func=discover_skills,
        name="discover_skills",
        description=(
            "从 Skill Registry 检索与用户目标相关的候选技能，只读取轻量 Manifest。"
            "当 Skill 较多、名称不确定或任务需要外部扩展能力时优先调用。检索不会授予执行权限。"
        ),
        args_schema=SkillDiscoveryInput,
        metadata={"capability": "pure", "resource": "skill_registry"},
    )


def retrieve_skill_manifests(
    query: str,
    *,
    top_k: int = 5,
    max_risk: str | None = None,
) -> List[Dict[str, Any]]:
    """Retrieve Skill manifests through the process-wide registry."""
    return _lazy_loader.retrieve_skill_manifests(
        query,
        top_k=top_k,
        max_risk=max_risk,
    )


def load_dynamic_skills(force_rescan: bool = False) -> List[StructuredTool]:
    """
    加载动态技能（懒加载 + 缓存版本）
    
    Args:
        force_rescan: 是否强制重新扫描技能目录（默认 False）
    
    Returns:
        工具对象列表（懒加载占位符）
    
    Note:
        - 启动时只扫描元数据，不加载完整内容
        - 首次调用技能时才加载完整内容
        - 支持热更新（修改技能文件后自动重新加载）
        - 使用 LRU 缓存策略
    """
    return _lazy_loader.get_all_tools(force_rescan=force_rescan)


def reload_skills() -> List[StructuredTool]:
    """
    强制重新扫描技能目录并清除缓存
    
    Returns:
        更新后的工具列表
    """
    return _lazy_loader.get_all_tools(force_rescan=True)


def get_skill_count() -> int:
    """
    获取当前技能数量（不触发加载）
    
    Returns:
        技能总数
    """
    return _lazy_loader.get_tool_count()


def clear_skill_cache():
    """清除技能内容缓存"""
    _lazy_loader.clear_cache()
