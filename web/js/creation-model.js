// Pure helpers for creating equipment from an empty or unfinished project (no DOM, no network).
//
// Examples are only filled into the request editor, never sent. They name tags (free ones, from the
// current model) but no dimensions or positions for the planner to copy, and they follow the model:
// an empty project offers a first vessel with its outlet valve (and a researched heat exchanger);
// an open outlet port offers continuing from it.

import { nextFreeTag } from './commands-model.js';

/** Required ports that no connection uses yet (the open ends of an unfinished arrangement), by port ID. */
export function openPorts(project) {
  const m = project.model;
  const used = new Set();
  for (const c of Object.values(m.connections)) {
    used.add(c.from_port_id);
    used.add(c.to_port_id);
  }
  return Object.values(m.ports).filter((p) => p && p.required && !used.has(p.id))
    .map((p) => ({ port: p, entity: m.entities[p.entity_id] })).filter((x) => x.entity)
    .sort((a, b) => String(a.port.id).localeCompare(String(b.port.id)));
}

/** Whether the model holds nothing at all (a genuinely blank project). */
export function isEmptyProject(project) {
  const m = project && project.model;
  return !!m && !Object.keys(m.entities).length && !Object.keys(m.connections).length && !Object.keys(m.ports).length;
}

/** Engineering-plan starting points for creation (see the module comment). */
export function creationExamples(project) {
  if (isEmptyProject(project)) {
    const tank = nextFreeTag(project, 'TK-');
    const valve = nextFreeTag(project, 'HV-');
    const exchanger = nextFreeTag(project, 'HX-');
    return [
      `Add a vertical storage tank ${tank} with an isolation valve ${valve} directly on its outlet; place both and route the line between them`,
      `Add a shell-and-tube heat exchanger ${exchanger} with separate shell-side and tube-side inlets and outlets; leave its connections open`,
    ];
  }
  const outlet = openPorts(project).find((x) => x.port.direction === 'out');
  if (!outlet) return [];
  const pump = nextFreeTag(project, 'P-');
  const valve = nextFreeTag(project, 'HV-');
  return [`Continue from ${outlet.entity.tag} ${outlet.port.name}: add a centrifugal pump ${pump} with its own suction isolation `
    + `valve ${valve} before it, and end the pump discharge at a scope boundary; place them and route each line`];
}
