export const meta = {
  name: 'oracle-verify',
  description: 'Adversarially verify insights still marked proposed (leftovers from interrupted runs)',
  whenToUse: 'args {repo, insights: [{insight_id, severity, kind}], votes_high?}',
  phases: [{ title: 'Verify', detail: 'refuters: opus for medium/high (2 votes for high), sonnet for info/low', model: 'opus' }],
}

const repo = args && args.repo
const items = (args && args.insights) || []
const VOTES_HIGH = (args && args.votes_high) || 2
if (!items.length) {
  log('No proposed insights passed; nothing to verify.')
  return { verified: [], rejected: [], unverified: [] }
}
log(`verifying ${items.length} proposed insights`)

const VERDICT = { type: 'object', properties: { refuted: { type: 'boolean' }, reason: { type: 'string' } },
  required: ['refuted', 'reason'] }
const REFUTE = {
  hidden_coupling: 'the pair is explained by an explicit import/call, test-with-subject, generated code or bulk edits',
  reverted_feature: 'the stated reason is not in the history, or the change was later re-applied',
  abandoned_branch: 'the branch work was merged under another name or the stated reason is invented',
  short_lived_experiment: 'the symbol was renamed/moved rather than abandoned',
  god_class: 'the class is a cohesive record/generated type or the metrics are wrong',
  cycle: 'the cycle is type-only/guarded and harmless',
  leaky_abstraction: 'the access is sanctioned (tests, friend module)',
  idiom: 'the cluster is boilerplate with no shared intent',
}

const verify = async (ins) => {
  const n = ins.severity === 'high' ? VOTES_HIGH : 1
  const deciding = n === 1
  // Opus only where a wrong verdict costs something; info/low findings get a Sonnet skeptic.
  const vModel = ins.severity === 'medium' || ins.severity === 'high' ? 'opus' : 'sonnet'
  const hint = REFUTE[ins.kind] || 'the citations do not show what is claimed'
  const votes = (await parallel(Array.from({ length: n }, (_, k) => () => agent(
    `You are skeptic #${k + 1} of ${n}. Try to REFUTE proposed Insight ${ins.insight_id}` +
    (repo ? ` (repo "${repo}")` : '') + '. Load it, run verify_citations, then re-derive the claim with ' +
    `get_commit_diff on every cited commit. Refute if ${hint}, if any citation does not show what is claimed, or ` +
    'if the claim overreaches. Default refuted=true if uncertain. ' +
    (deciding
      ? `You are the deciding verifier: finish by calling set_insight_status(insight_id="${ins.insight_id}", ` +
        'status = "verified" if not refuted else "rejected", verdict_notes = your reason, ' +
        `votes = [{"model": "${vModel}", "refuted": <bool>, "reason": <reason>}]).`
      : 'Do NOT call set_insight_status; only return your verdict.'),
    { phase: 'Verify', label: `verify:${ins.insight_id.slice(-6)}#${k + 1}`, agentType: 'oracle-verifier',
      model: vModel, effort: 'medium', schema: VERDICT },
  )))).filter(Boolean)
  if (votes.length < n) return { insight_id: ins.insight_id, status: 'unverified' }
  const survives = votes.every((v) => !v.refuted)
  if (!deciding) {
    await agent(
      `Call set_insight_status(insight_id="${ins.insight_id}", status="${survives ? 'verified' : 'rejected'}", ` +
      `verdict_notes=${JSON.stringify(votes.map((v) => v.reason).join(' | ').slice(0, 3500))}, ` +
      `votes=${JSON.stringify(votes.map((v) => ({ model: vModel, refuted: v.refuted, reason: v.reason.slice(0, 500) })))}` +
      '). Do not evaluate the insight yourself. Return "ok".',
      { phase: 'Verify', label: `record:${ins.insight_id.slice(-6)}`, agentType: 'oracle-verifier', model: 'sonnet',
        effort: 'low' })
  }
  return { insight_id: ins.insight_id, status: survives ? 'verified' : 'rejected' }
}

const out = (await pipeline(items, (ins) => verify(ins))).filter(Boolean)
const pick = (s) => out.filter((x) => x.status === s).map((x) => x.insight_id)
log(`verified ${pick('verified').length}, rejected ${pick('rejected').length}, unverified ${pick('unverified').length}`)
return { verified: pick('verified'), rejected: pick('rejected'), unverified: pick('unverified') }
