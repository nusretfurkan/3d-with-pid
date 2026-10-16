# Straight pipe sections and the equipment-body screen

Current guide to moving one straight section of a physical route (`move_route_segment`) and to the conservative screen that keeps new routes out of equipment bodies. The exact transform is specified once, in the module docstring of `skid/route_edit.py`; the screen is `skid/clearance.py`. The browser port (`web/js/pipe-sections.js`) must match the shared cases in `tests/fixtures/pipe-segments.json`. Source is authoritative.

## Editing unit and command

- A **section** is one maximal straight centreline leg after duplicate and same-direction collinear points are merged. Its index is valid only for the stored points at the command's `expected_revision` and is never persisted. CAD still has one `conn#pipe` sweep; this is editing granularity, not fabrication spools or welds.
- Payload exactly `{connection_id, segment_index, offset_m, assumption_id}`. The offset has one nonzero world-axis component, perpendicular to the section, with magnitude from 1 mm to 10 m (`MIN_OFFSET_M`, `MAX_OFFSET_M`). The route must be `current`.
- New points are computed from the stored route and the current port frames, never from client geometry. The result is a `manual`, `current` route citing an assumption added by the same command or batch operation (`assumption_not_fresh` otherwise). One revision, one undo step; a refusal changes nothing.
- Zero offset → `route_edit_no_op`; a nonzero offset below 1 mm → `route_edit_offset_too_small`. Neither creates history or an assumption.

## Geometry rules

- Both port positions and outward axes stay fixed. A moved interior section stretches an adjacent leg along the offset axis, or a connector is added.
- A moved terminal section keeps a fixed 0.16 m stub at its port (`TERMINAL_STUB_M`), so it must be longer than 0.16 m (0.32 m when one leg joins both ports).
- Checks run in a fixed order: at most 16 points, port tangency, legs of at least 1 mm, bend room (radius `min(1.5 DN, 0.49 × shorter adjacent run)` must exceed 1.01 × pipe radius), at least one outside diameter between non-adjacent legs, then body clearance.
- Ports must face along a world axis.

## Equipment-body screen

- Both sides are conservative world axis-aligned boxes. Route boxes cover legs grown by the pipe radius, each bend's tangency triangle and each end flange. Body boxes are per built-in template primitive or per resolved composed part, for every placed entity, endpoint owners included. The only exemption is the built-in indicator's mount stem; its dial still blocks.
- A `finned_frame` is bounded by radius `hypot(fin_tip_radius, fin_thickness / 2)`, because fin corners lie beyond the tip radius.
- `loft`, `volute_casing` and unknown parts have no proven bound. New routes or moves that would need them are refused with `route_clearance_unavailable` (or `reroute_clearance_unavailable`).

Where it applies:

| Operation | Behaviour |
| --- | --- |
| `set_route`, `move_route_segment` | New intrusions refused (`route_intrusion`, `route_edit_intrusion`). |
| `insert_valve`, `insert_equipment` | Each explicit current split half is screened after the inserted item is placed; an intrusion refuses the whole command. Omitted halves stay pending. |
| `connect_ports` with a route | Screened when submitted and again in the final batch result. |
| `batch` | Every route its operations submitted that is still current is re-screened in the final result. |
| Guided reroute | Intruding candidates are discarded. |
| Existing stored routes | Never refused. They load and stay editable with a `route_body_intrusion` warning, so outputs stay honestly pending until repaired. |

## Refusal codes

`route_edit_unknown_section`, `route_edit_invalid_offset`, `route_edit_no_op`, `route_edit_offset_parallel`, `route_edit_offset_too_small`, `route_edit_offset_too_large`, `route_edit_stub_too_short`, `route_edit_too_many_points`, `route_edit_port_tangent`, `route_edit_short_leg`, `route_edit_bend_room`, `route_edit_self_clearance`, `route_edit_intrusion`, `route_edit_reverses`, `route_edit_not_orthogonal`, `route_edit_zero_leg`, `route_edit_not_current`, `route_edit_no_route`, `route_edit_unplaced_endpoint`, `route_edit_frame_unavailable`, `route_edit_port_direction_unsupported`, plus the core's `invalid_value`, `unsupported_payload`, `unknown_reference`, `stale_revision`, `assumption_not_fresh`, `route_intrusion` and `route_clearance_unavailable`.

## Service and UI

The service advertises `state.capabilities.pipe_segment_move = true`; commands use the existing `POST /api/command`. In the browser, enable Move in a 3D pane, select a straight pipe surface or a numbered section, then drag a perpendicular arrow or type an offset. Release only previews; Apply sends one command. The browser cannot run the body screen, so such refusals arrive on Apply ([UI](UI.md)).

## Limits

- Built-in bodies are template primitives; detailed CAD features beyond them (flange rims, handwheels, base details) are not screened. `tests/test_route_edit.py` also checks selected repairs against actual OCCT solids.
- Boxes over-approximate; a refused route can be a near miss.
- Not checked: pipe-to-pipe clashes, equipment-to-equipment overlap, supports, stress, access, maintenance space and fabrication. This is not automatic plant routing.
