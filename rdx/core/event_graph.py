"""
Event tree 构建与导航 service。

将 RenderDoc replay controller 的扁平/递归 ``ActionDescription`` 树
转换为可序列化的 :class:`~rdx.models.EventNode` 树，并提供查询、展开、
以及在缺少显式 debug markers 时推断 render-pass 边界的辅助方法。

Usage::

    from rdx.core.event_graph import EventGraphService

    svc = EventGraphService()
    tree = svc.build_event_tree(session_id, session_manager)
    draws = svc.get_draw_events(tree)
    lo, hi = svc.get_event_range(tree)
    tree = svc.infer_passes(tree, session_id, session_manager)
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from rdx.models import EventFlags, EventNode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import（延迟导入）
# ---------------------------------------------------------------------------


def _get_rd():
    """返回 ``renderdoc`` module，并在首次调用时导入。"""
    import renderdoc as rd
    return rd


# ---------------------------------------------------------------------------
# Internal helpers（内部辅助）
# ---------------------------------------------------------------------------


def _has_flag(flags: Any, flag: Any) -> bool:
    """安全判断 bitfield *flags* 是否包含 *flag*。

    同时支持 Python :class:`enum.IntFlag` 与 renderdoc module
    暴露的 SWIG 生成 ``ActionFlags`` 类型。
    """
    if flag is None:
        return False
    try:
        return bool(flags & flag)
    except TypeError:
        return False


def _first_attr(obj: Any, *names: str) -> Any:
    """Return the first attribute found on *obj* from *names* (or None)."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _map_action_flags(flags: Any) -> EventFlags:
    """将 RenderDoc ``ActionFlags`` bitfield 转换为 :class:`EventFlags`。"""
    rd = _get_rd()
    af = rd.ActionFlags

    # RenderDoc bindings expose ActionFlags.Drawcall (C++ name) not ActionFlags.Draw.
    # Be tolerant to older/alternate naming to avoid hard failures when enumerating actions.
    drawcall = _first_attr(af, "Drawcall", "Draw")
    dispatch = _first_attr(af, "Dispatch")
    mesh_dispatch = _first_attr(af, "MeshDispatch")
    dispatch_ray = _first_attr(af, "DispatchRay")
    build_acc_struct = _first_attr(af, "BuildAccStruct")
    pass_boundary = _first_attr(af, "PassBoundary")
    return EventFlags(
        is_draw=_has_flag(flags, drawcall) or _has_flag(flags, mesh_dispatch),
        is_dispatch=(
            _has_flag(flags, dispatch)
            or _has_flag(flags, mesh_dispatch)
            or _has_flag(flags, dispatch_ray)
            or _has_flag(flags, build_acc_struct)
        ),
        is_marker=(
            _has_flag(flags, _first_attr(af, "SetMarker"))
            or _has_flag(flags, _first_attr(af, "PushMarker"))
            or _has_flag(flags, _first_attr(af, "PopMarker"))
        ),
        is_copy=_has_flag(flags, _first_attr(af, "Copy")),
        is_resolve=_has_flag(flags, _first_attr(af, "Resolve")),
        is_clear=_has_flag(flags, _first_attr(af, "Clear")),
        is_pass_boundary=_has_flag(flags, _first_attr(af, "Present")) or _has_flag(flags, pass_boundary),
    )


def _resource_id_str(rid: Any) -> str:
    """将 RenderDoc ``ResourceId`` 转成确定性的字符串 key。"""
    try:
        return str(int(rid))
    except (TypeError, ValueError):
        return str(rid)


def _query_output_targets(controller: Any, event_id: int) -> Tuple[str, ...]:
    """跳转到 *event_id* 并读取当前 output targets。

    当 pipeline-state 访问器不可用时（例如旧版本 RenderDoc
    或特定 API backend），会优雅降级。
    """
    try:
        rd = _get_rd()
        controller.SetFrameEvent(event_id, True)
        pipe = controller.GetPipelineState()
        null_id = rd.ResourceId.Null()
        targets: list[str] = []

        # Colour / render targets
        try:
            for desc in pipe.GetOutputTargets():
                rid = getattr(desc, "resourceId", None)
                if rid is not None and rid != null_id:
                    targets.append(_resource_id_str(rid))
        except (AttributeError, TypeError):
            pass

        # Depth target
        try:
            depth = pipe.GetDepthTarget()
            if depth is not None:
                rid = getattr(depth, "resourceId", None)
                if rid is not None and rid != null_id:
                    targets.append(_resource_id_str(rid))
        except (AttributeError, TypeError):
            pass

        return tuple(sorted(targets))
    except Exception as exc:
        logger.debug(
            "Could not query output targets for event %d: %s",
            event_id, exc,
        )
        return ()


# ---------------------------------------------------------------------------
# Public service（对外服务）
# ---------------------------------------------------------------------------

class EventGraphService:
    """构建并查询 RenderDoc capture 的 event tree。

    所有方法均为同步；如有需要，调用方应将其提交到 thread pool
    （``SessionManager`` 已对内部调用做了处理）。
    """

    # -- Tree construction（树构建）-----------------------------------------

    def build_event_tree(
        self,
        session_id: str,
        session_manager: Any,
    ) -> List[EventNode]:
        """从 replay controller 构建 :class:`EventNode` 树。

        Parameters
        ----------
        session_id:
            已打开 capture 的活跃 session。
        session_manager:
            用于获取 ``IReplayController`` 的
            :class:`~rdx.core.session_manager.SessionManager` 实例。

        Returns
        -------
        list[EventNode]
            顶层节点列表，其 ``children`` 组成完整树。
        """
        controller = session_manager.get_controller(session_id)
        root_actions = controller.GetRootActions()
        nodes = [
            self._build_node(action, depth=0)
            for action in root_actions
        ]
        total = self._count_nodes(nodes)
        logger.info(
            "Built event tree for session %s: %d total nodes", session_id, total,
        )
        return nodes

    # -- Querying（查询）----------------------------------------------------

    def get_draw_events(self, event_tree: List[EventNode]) -> List[EventNode]:
        """展开树并只返回 draw/dispatch events。

        返回列表保持文档顺序（depth-first pre-order）。
        """
        result: List[EventNode] = []
        self._collect_draws(event_tree, result)
        return result

    def get_event_range(
        self,
        event_tree: List[EventNode],
    ) -> Tuple[int, int]:
        """返回全树范围内的 ``(min_event_id, max_event_id)``。

        对空树返回 ``(0, 0)``。
        """
        ids: List[int] = []
        self._collect_ids(event_tree, ids)
        if not ids:
            return (0, 0)
        return (min(ids), max(ids))

    def find_event(
        self,
        event_tree: List[EventNode],
        event_id: int,
    ) -> Optional[EventNode]:
        """按 *event_id* 深度优先搜索 :class:`EventNode`。

        若 event 不存在则返回 ``None``。
        """
        for node in event_tree:
            if node.event_id == event_id:
                return node
            found = self.find_event(node.children, event_id)
            if found is not None:
                return found
        return None

    def get_event_path(
        self,
        event_tree: List[EventNode],
        event_id: int,
    ) -> List[int]:
        """返回从树根到 *event_id* 的路径。

        返回列表包含从顶层节点到目标节点（含目标）的所有 ``event_id``。
        若找不到 *event_id* 则返回空列表。
        """
        path: List[int] = []
        if self._build_path(event_tree, event_id, path):
            return path
        return []

    # -- Pass inference（Pass 推断）----------------------------------------

    def infer_passes(
        self,
        event_tree: List[EventNode],
        session_id: str,
        session_manager: Any,
    ) -> List[EventNode]:
        """根据 output-target 的变化推断 render-pass 边界。

        当 capture 缺少显式 debug markers 时，本方法会把连续的
        draw/dispatch 调用按相同 render targets 分组为逻辑 pass。
        每个分组都会分配一个合成 label（``pass_1``, ``pass_2`` ...），
        并写入 :attr:`EventNode.inferred_pass`。

        *event_tree* 会被原地修改，并同时返回以便使用。

        Parameters
        ----------
        event_tree:
            由 :meth:`build_event_tree` 构建的树。
        session_id:
            活跃 session（当节点的 ``output_targets`` 为空时，
            用于查询 pipeline state）。
        session_manager:
            :class:`~rdx.core.session_manager.SessionManager` 实例。

        Returns
        -------
        list[EventNode]
            填充了 ``inferred_pass`` 字段的同一份 *event_tree*。
        """
        draws = self.get_draw_events(event_tree)
        if not draws:
            return event_tree

        controller = session_manager.get_controller(session_id)

        # 解析每个 draw 的有效 output-target 集合。
        resolved: List[Tuple[EventNode, Tuple[str, ...]]] = []
        for node in draws:
            targets = tuple(sorted(node.output_targets))
            if not targets:
                targets = _query_output_targets(controller, node.event_id)
            resolved.append((node, targets))

        # 遍历列表，当相邻 draw 的 target 集变化时递增 pass 计数。
        pass_index = 0
        prev_targets: Optional[Tuple[str, ...]] = None
        for node, targets in resolved:
            if targets != prev_targets:
                pass_index += 1
                prev_targets = targets
            node.inferred_pass = f"pass_{pass_index}"

        logger.info(
            "Inferred %d passes from %d draw events in session %s",
            pass_index, len(draws), session_id,
        )
        return event_tree

    # -- Private helpers: tree walking（树遍历）------------------------------

    def _build_node(self, action: Any, depth: int) -> EventNode:
        """递归地将 ``ActionDescription`` 转换为 ``EventNode``。"""
        flags = _map_action_flags(action.flags)

        raw_outputs = getattr(action, "outputs", None) or []
        output_targets = [_resource_id_str(rid) for rid in raw_outputs]

        raw_children = getattr(action, "children", None) or []
        children = [
            self._build_node(child, depth=depth + 1)
            for child in raw_children
        ]

        name = getattr(action, "customName", None) or ""

        return EventNode(
            event_id=int(action.eventId),
            name=str(name),
            flags=flags,
            children=children,
            depth=depth,
            output_targets=output_targets,
        )

    @staticmethod
    def _collect_draws(
        nodes: List[EventNode],
        result: List[EventNode],
    ) -> None:
        """深度优先收集 draw/dispatch 节点。"""
        for node in nodes:
            if node.flags.is_draw or node.flags.is_dispatch:
                result.append(node)
            EventGraphService._collect_draws(node.children, result)

    @staticmethod
    def _collect_ids(
        nodes: List[EventNode],
        ids: List[int],
    ) -> None:
        """深度优先收集所有 event ID。"""
        for node in nodes:
            ids.append(node.event_id)
            EventGraphService._collect_ids(node.children, ids)

    @staticmethod
    def _count_nodes(nodes: List[EventNode]) -> int:
        """返回树中的节点总数。"""
        total = 0
        for node in nodes:
            total += 1
            total += EventGraphService._count_nodes(node.children)
        return total

    def _build_path(
        self,
        nodes: List[EventNode],
        target_id: int,
        path: List[int],
    ) -> bool:
        """用从 root 到 *target_id* 的 event IDs 填充 *path*。

        找到目标返回 ``True``，否则返回 ``False``。调用方仅应在
        返回值为 ``True`` 时将 *path* 视为有效。
        """
        for node in nodes:
            path.append(node.event_id)
            if node.event_id == target_id:
                return True
            if self._build_path(node.children, target_id, path):
                return True
            path.pop()
        return False
