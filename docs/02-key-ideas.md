# Key ideas

> The vocabulary used throughout these docs, each term explained in plain words. Skim it now, and come back when a
> later page uses a word you don't know.

## Git basics

- **Repository (repo).** A project's code plus its complete history.
- **Commit.** A saved snapshot of the code, with a message, an author and a date. Each commit has a **hash**: a
  40-character fingerprint such as `d44f1c65…`, usually shortened to 7–10 characters.
- **Parent.** The commit that came before. A normal commit has one parent; a **merge commit** has two, joining two lines
  of work.
- **Branch.** A named line of work. Most repositories have a **default branch** (often `main`) that holds the official
  code.
- **Main line.** Every commit reachable from the default branch. Oracle trusts main-line history more, because work
  inside an abandoned branch is often experimentation.
- **Diff.** The difference between two versions: lines added (`+`) and lines removed (`-`). A diff is divided into
  **hunks**, which are runs of nearby changed lines.
- **Revert.** A commit that undoes an earlier commit.
- **Pull request (PR).** A proposal on GitHub to merge a branch, usually with a discussion. A **squash merge** turns a
  whole PR into a single commit, typically with a subject ending in `(#1234)`.
- **Tag.** A name pinned to a commit, usually a release such as `v2.3.0`.
- **Author date and committer date.** When a change was written, versus when it was last rewritten (a rebase resets
  the committer date). Neither is when it reached `main`, which Oracle calls the **landing date**.

## Code analysis basics

- **Syntax tree.** The structured form of source code that a parser produces: "this is a class, containing these
  functions, which call these names". Oracle uses the **tree-sitter** parser for Python and TypeScript/JavaScript.
- **Symbol.** A named piece of code: a class, function, method, interface or module-level variable.
- **Qualified name (qualname).** A symbol's name including its containers, such as `Flask.wsgi_app` for the method
  `wsgi_app` in class `Flask`.
- **Symbol uid.** Oracle's unique id for a symbol, in the form `sym:<repo>:<path>:<qualname>:<kind>`, for example
  `sym:flask:src/flask/app.py:Flask.wsgi_app:function`.
- **Call graph.** Who calls whom: an edge from function A to function B means A calls B.
- **Import graph.** Which file imports which.
- **Blast radius.** Everything a change could affect, starting with the callers of the changed code.
- **Public API.** Code meant to be used from outside the project. Changing it can break other people's code: that's
  a **breaking change**.

## Oracle's own terms

- **Ingestion.** Reading a repository into Oracle's store. It is deterministic and uses no AI.
- **Store.** A per-repository database file (SQLite) holding everything Oracle computed. It is the single source of
  truth.
- **Graph and vectors.** Two extra copies built from the store, for fast queries:
  - **Neo4j**, a graph database: "which files are connected to this one?"
  - **Qdrant**, a vector database for search by meaning: "find code similar to this description".
- **Co-change.** Two files that are often modified in the same commits. Measured with **Jaccard similarity**, the
  share of commits touching either file that touch both.
- **Hidden coupling.** Co-change between files with no import or call connecting them. It's a sign of an unwritten
  rule between them.
- **Candidate.** Something a program flagged as possibly interesting: a revert, a deleted function, a big class, a
  coupled pair.
- **Work packet.** A small bundle of evidence about one candidate, sized for an AI agent to read: the metrics, commit
  messages and trimmed diffs.
- **Insight.** A finding written by an AI agent about a packet. It must cite commits, files and symbols.
- **Citation gate.** A program that rejects any insight whose citations don't check out against git.
- **Verification.** A second agent tries to *refute* each insight. Insights go **proposed → verified** or
  **→ rejected**.
- **Finding.** A hazard flagged by a change-safety detector, with a **severity**:
  - **block**: must be fixed or explicitly accepted
  - **warn**: should be reviewed
  - **info**: context only
- **Contract.** A link between two pieces of code that exists only through a shared string, such as an environment
  variable name or an event name.

## Claude Code terms

- **MCP (Model Context Protocol).** The standard way to give Claude Code extra tools. Oracle runs an **MCP server**
  named `afd-oracle`.
- **Skill.** Instructions Claude loads automatically when relevant. Oracle's skill tells Claude when to use its tools.
- **Slash command.** A typed command such as `/afd oracle check`.
- **Agent / subagent.** A separate Claude instance given one focused job.
- **Workflow.** A script that runs many agents in a planned order.

Next: [Architecture](03-architecture.md)
