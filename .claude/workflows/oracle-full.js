export const meta = {
  name: 'oracle-full',
  description: 'Full oracle pass: coupling + graveyard + pattern + catalogue miners (each self-verifying), then an Opus synthesis report',
  whenToUse: 'args {repo, run_id, coupling_ids, graveyard_ids, pattern_ids, catalog_ids?, batch_size?, report_path?}',
  phases: [
    { title: 'Mine', detail: 'four child workflows in parallel (sonnet analysts, opus verifiers)' },
    { title: 'Synthesize', detail: 'one opus agent over verified insights only', model: 'opus' },
    { title: 'Critique', detail: 'completeness critic', model: 'sonnet' },
  ],
}

const a = args || {}
const repo = a.repo
const RUN = a.run_id || 'oracle-full'
const common = { repo, batch_size: a.batch_size || 5, votes_high: a.votes_high || 2 }
const report = a.report_path || `reports/${repo || 'repo'}-oracle.md`
const sizes = { coupling: (a.coupling_ids || []).length, graveyard: (a.graveyard_ids || []).length,
  pattern: (a.pattern_ids || []).length, catalog: (a.catalog_ids || []).length }
log(`packets: coupling ${sizes.coupling}, graveyard ${sizes.graveyard}, pattern ${sizes.pattern}, catalog ${sizes.catalog}`)

phase('Mine')
const [coupling, graveyard, patterns, catalog] = await parallel([
  async () => (sizes.coupling ? workflow('oracle-coupling', { ...common, packet_ids: a.coupling_ids, run_id: `${RUN}/coupling` }) : null),
  async () => (sizes.graveyard ? workflow('oracle-graveyard', { ...common, packet_ids: a.graveyard_ids, run_id: `${RUN}/graveyard` }) : null),
  async () => (sizes.pattern ? workflow('oracle-patterns', { ...common, packet_ids: a.pattern_ids, run_id: `${RUN}/patterns` }) : null),
  async () => (sizes.catalog ? workflow('oracle-catalog', { ...common, packet_ids: a.catalog_ids, run_id: `${RUN}/catalog` }) : null),
])
const children = { coupling, graveyard, patterns, catalog }
const verified = Object.values(children).filter(Boolean).flatMap((c) => c.verified || [])
const unverified = Object.values(children).filter(Boolean).flatMap((c) => c.unverified || [])
log(`verified insights: ${verified.length}; unverified (left proposed): ${unverified.length}`)

phase('Synthesize')
const synthesis = verified.length
  ? await agent(
    `Write an architecture oracle report for repo "${repo}" to ${report} (Markdown). Use ONLY verified ` +
    'insights: call list_insights(status="verified"' + (repo ? `, repo="${repo}"` : '') + ', limit=500) and, to ' +
    'cross-link findings, search_qdrant(collection="architectural_insights") and run_cypher over ' +
    '(:Insight)-[:EVIDENCED_BY|CONCERNS]->() neighbourhoods. Structure: 1) executive summary (5 bullets max); ' +
    '2) hidden coupling; 3) rejected attempts (what was tried, why it failed, files); 4) anti-patterns and ' +
    'recurring patterns; 5) catalogue findings grouped by theme (infrastructure, configuration, data and ' +
    'schema, reliability, testing, security, dependencies, team knowledge); 6) cross-links (e.g. a rejected attempt that explains a current coupling, a god class ' +
    'that sits at the centre of several couplings); 7) recommended next steps. Every claim must carry its ' +
    'insight id and short commit hashes in brackets. Do not introduce findings that are not verified insights. ' +
    'Return the report path and a 3-sentence summary.',
    { phase: 'Synthesize', label: 'synthesis', model: 'opus', effort: 'high' })
  : 'no verified insights; report skipped'

phase('Critique')
const GAPS = { type: 'object', properties: { gaps: { type: 'array', items: { type: 'string' } } }, required: ['gaps'] }
const critic = await agent(
  `Completeness check for oracle run ${RUN}. Child results: ${JSON.stringify({ sizes, children }).slice(0, 6000)}. ` +
  'Call get_work_packets for kinds coupling, graveyard, pattern and catalog with ids_only=true, claim=false, limit=500' +
  (repo ? `, repo="${repo}"` : '') + ' to count packets still open. List concrete gaps: packet kinds never run, ' +
  'open packets left, failed batches, unverified insights, and anything the report could not cover. Be brief.',
  { phase: 'Critique', label: 'completeness', model: 'sonnet', effort: 'low', schema: GAPS })

return { sizes, children, verified_count: verified.length, unverified, report, synthesis,
  gaps: critic ? critic.gaps : ['critic failed'] }
