import React, { useEffect, useMemo, useState } from "react";
import Layout from "@theme/Layout";
import styles from "./status.module.css";

/**
 * Integration Status — public dashboard showing red/green health for every
 * integration Agent Kernel supports, derived from the latest GitHub Actions
 * runs on the develop branch.
 *
 * Data is published by the `publish-status` job of each test workflow to the
 * orphan `status-data` branch (see docs/specs/integration-status.md):
 *   status/<workflow>.json   - latest tile statuses
 *   history/<workflow>.jsonl - one compact line per superseded run
 */

const DATA_BASE =
  "https://raw.githubusercontent.com/yaalalabs/agent-kernel/status-data";

const WORKFLOW_KEYS = ["test", "integration-test", "integration-test-weekly"];

const SOURCE_TITLES: Record<string, string> = {
  test: "Core tests (per commit)",
  "integration-test": "Messaging integrations (nightly)",
  "integration-test-weekly": "Cloud deployments (weekly)",
};

const CATEGORY_ORDER = [
  "Core & Frameworks",
  "API Features",
  "Multimodal",
  "Guardrails",
  "Messaging Integrations",
  "Agent Memory / Knowledge",
  "AWS Serverless",
  "AWS Containerized",
  "Azure Serverless",
  "Azure Containerized",
  "GCP Serverless",
  "GCP Containerized",
  "Core & Examples",
];

const HISTORY_STRIP_LENGTH = 10;

type TileStatus = "pass" | "fail" | "skipped" | "unknown";

interface StatusResult {
  path: string;
  type: string;
  category: string;
  label: string;
  description: string | null;
  status: TileStatus;
  job_url: string | null;
}

interface StatusDoc {
  workflow: string;
  workflow_name: string;
  run_id: number;
  run_url: string;
  branch: string;
  commit: string;
  completed_at: string;
  expected_cadence_hours: number | null;
  results: StatusResult[];
}

interface HistoryLine {
  run_id: number;
  run_url: string;
  commit: string;
  completed_at: string;
  results: Record<string, string>;
}

interface Tile extends StatusResult {
  workflow: string;
  runUrl: string;
  completedAt: string;
  stale: boolean;
  history: { status: TileStatus; runUrl: string; completedAt: string }[];
  failingSince: string | null;
}

function tileKey(category: string, label: string): string {
  return `${category}|${label}`;
}

function relativeTime(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "just now";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `${Math.max(minutes, 1)} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  return `${days} d ago`;
}

function isStale(doc: StatusDoc): boolean {
  if (doc.expected_cadence_hours == null) return false;
  const ageHours =
    (Date.now() - new Date(doc.completed_at).getTime()) / 3_600_000;
  return ageHours > doc.expected_cadence_hours;
}

function parseHistory(text: string): HistoryLine[] {
  return text
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => {
      try {
        return JSON.parse(line) as HistoryLine;
      } catch {
        return null;
      }
    })
    .filter((line): line is HistoryLine => line !== null);
}

function buildTiles(doc: StatusDoc, history: HistoryLine[]): Tile[] {
  const stale = isStale(doc);
  // Oldest first, so strips read left (old) to right (new)
  const orderedHistory = [...history].sort((a, b) => a.run_id - b.run_id);

  return doc.results.map((result) => {
    const key = tileKey(result.category, result.label);
    const past = orderedHistory
      .filter((line) => key in line.results)
      .map((line) => ({
        status: line.results[key] as TileStatus,
        runUrl: line.run_url,
        completedAt: line.completed_at,
      }));

    const strip = [
      ...past,
      { status: result.status, runUrl: doc.run_url, completedAt: doc.completed_at },
    ].slice(-HISTORY_STRIP_LENGTH);

    let failingSince: string | null = null;
    if (result.status === "fail") {
      failingSince = doc.completed_at;
      for (let i = past.length - 1; i >= 0; i--) {
        if (past[i].status !== "fail") break;
        failingSince = past[i].completedAt;
      }
    }

    return {
      ...result,
      workflow: doc.workflow,
      runUrl: doc.run_url,
      completedAt: doc.completed_at,
      stale,
      history: strip,
      failingSince,
    };
  });
}

/** Sample data for local development (`npm start`) before the status-data
 * branch exists, and for styling work via ?mock=1. */
function mockDocs(): StatusDoc[] {
  const now = new Date().toISOString();
  const weekAgo = new Date(Date.now() - 12 * 86_400_000).toISOString();
  const result = (
    category: string,
    label: string,
    status: TileStatus
  ): StatusResult => ({
    path: "examples/mock",
    type: "api",
    category,
    label,
    description: "Mock data — the status-data branch is not reachable",
    status,
    job_url: null,
  });
  return [
    {
      workflow: "test",
      workflow_name: "Test",
      run_id: 1,
      run_url: "https://github.com/yaalalabs/agent-kernel/actions",
      branch: "develop",
      commit: "mock0000",
      completed_at: now,
      expected_cadence_hours: null,
      results: [
        result("Core & Frameworks", "OpenAI (CLI)", "pass"),
        result("Core & Frameworks", "CrewAI (CLI)", "fail"),
        result("API Features", "Hooks", "pass"),
        result("Messaging Integrations", "Slack", "pass"),
        result("Agent Memory / Knowledge", "Key-value cache memory", "skipped"),
      ],
    },
    {
      workflow: "integration-test-weekly",
      workflow_name: "Weekly Integration Tests",
      run_id: 2,
      run_url: "https://github.com/yaalalabs/agent-kernel/actions",
      branch: "develop",
      commit: "mock0000",
      completed_at: weekAgo,
      expected_cadence_hours: 192,
      results: [
        result("AWS Serverless", "LangGraph on Lambda", "pass"),
        result("Agent Memory / Knowledge", "Cosmos DB memory", "pass"),
        result("Azure Serverless", "OpenAI + Cosmos memory", "pass"),
      ],
    },
  ];
}

function shouldMock(): boolean {
  if (typeof window === "undefined") return false;
  return (
    window.location.search.includes("mock=1") ||
    window.location.hostname === "localhost"
  );
}

const STATUS_DOT: Record<TileStatus, string> = {
  pass: styles.dotPass,
  fail: styles.dotFail,
  skipped: styles.dotSkipped,
  unknown: styles.dotUnknown,
};

const STATUS_CELL: Record<TileStatus, string> = {
  pass: styles.historyPass,
  fail: styles.historyFail,
  skipped: styles.historySkipped,
  unknown: styles.historyUnknown,
};

function TileCard({ tile }: { tile: Tile }) {
  const tooltip = [
    tile.description,
    `Example: ${tile.path}`,
    `Status: ${tile.status}${tile.stale ? " (stale)" : ""}`,
    `Last run: ${new Date(tile.completedAt).toLocaleString()}`,
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <a
      className={styles.tile}
      href={tile.job_url ?? tile.runUrl}
      target="_blank"
      rel="noopener noreferrer"
      title={tooltip}
    >
      <div className={styles.tileHeader}>
        <span className={`${styles.dot} ${STATUS_DOT[tile.status]}`} />
        <span className={styles.tileLabel}>{tile.label}</span>
        {tile.stale && <span className={styles.staleBadge}>stale</span>}
      </div>
      <div className={styles.tileMeta}>
        {tile.status === "fail" && tile.failingSince ? (
          <span className={styles.failingSince}>
            failing since {new Date(tile.failingSince).toLocaleDateString()}
          </span>
        ) : (
          <span>updated {relativeTime(tile.completedAt)}</span>
        )}
      </div>
      <div className={styles.historyStrip}>
        {Array.from({ length: HISTORY_STRIP_LENGTH }).map((_, index) => {
          const cell =
            tile.history[index + tile.history.length - HISTORY_STRIP_LENGTH];
          return (
            <span
              key={index}
              className={`${styles.historyCell} ${
                cell ? STATUS_CELL[cell.status] : ""
              }`}
              title={
                cell
                  ? `${cell.status} — ${new Date(cell.completedAt).toLocaleString()}`
                  : "no data"
              }
            />
          );
        })}
      </div>
    </a>
  );
}

export default function StatusPage() {
  const [docs, setDocs] = useState<StatusDoc[] | null>(null);
  const [histories, setHistories] = useState<Record<string, HistoryLine[]>>({});
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      const statusResults = await Promise.allSettled(
        WORKFLOW_KEYS.map(async (key) => {
          const response = await fetch(`${DATA_BASE}/status/${key}.json`);
          if (!response.ok) throw new Error(`${key}: ${response.status}`);
          return (await response.json()) as StatusDoc;
        })
      );
      const historyResults = await Promise.allSettled(
        WORKFLOW_KEYS.map(async (key) => {
          const response = await fetch(`${DATA_BASE}/history/${key}.jsonl`);
          if (!response.ok) throw new Error(`${key}: ${response.status}`);
          return { key, lines: parseHistory(await response.text()) };
        })
      );
      if (cancelled) return;

      const loadedDocs = statusResults
        .filter(
          (result): result is PromiseFulfilledResult<StatusDoc> =>
            result.status === "fulfilled"
        )
        .map((result) => result.value);

      const loadedHistories: Record<string, HistoryLine[]> = {};
      for (const result of historyResults) {
        if (result.status === "fulfilled") {
          loadedHistories[result.value.key] = result.value.lines;
        }
      }

      if (loadedDocs.length === 0) {
        if (shouldMock()) {
          setDocs(mockDocs());
        } else {
          setFailed(true);
        }
      } else {
        setDocs(loadedDocs);
        setHistories(loadedHistories);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const tiles = useMemo(() => {
    if (!docs) return [];
    return docs.flatMap((doc) => buildTiles(doc, histories[doc.workflow] ?? []));
  }, [docs, histories]);

  const categories = useMemo(() => {
    const grouped = new Map<string, Tile[]>();
    for (const tile of tiles) {
      const list = grouped.get(tile.category) ?? [];
      list.push(tile);
      grouped.set(tile.category, list);
    }
    const known = CATEGORY_ORDER.filter((category) => grouped.has(category));
    const extra = [...grouped.keys()]
      .filter((category) => !CATEGORY_ORDER.includes(category))
      .sort();
    return [...known, ...extra].map((category) => ({
      category,
      tiles: (grouped.get(category) ?? []).sort((a, b) =>
        a.label.localeCompare(b.label)
      ),
    }));
  }, [tiles]);

  const summary = useMemo(() => {
    const passing = tiles.filter((tile) => tile.status === "pass").length;
    const failing = tiles.filter((tile) => tile.status === "fail").length;
    const stale = tiles.filter((tile) => tile.stale).length;
    const other = tiles.length - passing - failing;
    return { passing, failing, stale, other };
  }, [tiles]);

  return (
    <Layout
      title="Integration Status"
      description="Live health of Agent Kernel's framework, cloud, memory, and messaging integrations, based on the latest test runs on the develop branch."
    >
      <main className={styles.page}>
        <div className={styles.header}>
          <h1>Integration Status</h1>
          <p className={styles.subtitle}>
            Live health of every integration Agent Kernel supports — agent
            frameworks, cloud deployment variants, memory and knowledge
            backends, and messaging platforms — based on the latest test
            pipeline runs on the <code>develop</code> branch. Every tile links
            to the GitHub Actions run that produced it.
          </p>

          {docs && (
            <>
              <div className={styles.summaryRow}>
                <span className={styles.summaryChip}>
                  <span className={`${styles.dot} ${styles.dotPass}`} />
                  {summary.passing} passing
                </span>
                <span className={styles.summaryChip}>
                  <span className={`${styles.dot} ${styles.dotFail}`} />
                  {summary.failing} failing
                </span>
                {summary.other > 0 && (
                  <span className={styles.summaryChip}>
                    <span className={`${styles.dot} ${styles.dotSkipped}`} />
                    {summary.other} skipped / unknown
                  </span>
                )}
                {summary.stale > 0 && (
                  <span className={styles.summaryChip}>
                    <span className={styles.staleBadge}>stale</span>
                    {summary.stale} stale
                  </span>
                )}
              </div>
              <ul className={styles.sources}>
                {docs.map((doc) => (
                  <li key={doc.workflow}>
                    {SOURCE_TITLES[doc.workflow] ?? doc.workflow_name}: last run{" "}
                    <a href={doc.run_url} target="_blank" rel="noopener noreferrer">
                      {relativeTime(doc.completed_at)}
                    </a>{" "}
                    (commit <code>{doc.commit}</code>)
                    {isStale(doc) && (
                      <span className={styles.staleSource}> — stale</span>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>

        {failed && (
          <div className={styles.notice}>
            Status data is not available right now. It is published by the test
            workflows on the develop branch — see the{" "}
            <a
              href="https://github.com/yaalalabs/agent-kernel/actions"
              target="_blank"
              rel="noopener noreferrer"
            >
              GitHub Actions runs
            </a>{" "}
            directly.
          </div>
        )}

        {!docs && !failed && (
          <div className={styles.grid}>
            {Array.from({ length: 12 }).map((_, index) => (
              <div key={index} className={styles.skeleton} />
            ))}
          </div>
        )}

        {categories.map(({ category, tiles: categoryTiles }) => (
          <section key={category} className={styles.section}>
            <h2>{category}</h2>
            <div className={styles.grid}>
              {categoryTiles.map((tile) => (
                <TileCard
                  key={tileKey(tile.category, tile.label)}
                  tile={tile}
                />
              ))}
            </div>
          </section>
        ))}

        {docs && (
          <>
            <div className={styles.legend}>
              <span className={styles.legendItem}>
                <span className={`${styles.dot} ${styles.dotPass}`} /> passing
              </span>
              <span className={styles.legendItem}>
                <span className={`${styles.dot} ${styles.dotFail}`} /> failing
              </span>
              <span className={styles.legendItem}>
                <span className={`${styles.dot} ${styles.dotSkipped}`} />{" "}
                skipped / no data
              </span>
              <span className={styles.legendItem}>
                <span className={styles.staleBadge}>stale</span> last run older
                than its expected cadence
              </span>
            </div>
            <p className={styles.footerNote}>
              Each tile's strip shows its most recent runs, oldest to newest.
              Statuses come from the Test, Nightly Integration Tests, and
              Weekly Integration Tests workflows; tests are categorized via the
              config files in{" "}
              <a
                href="https://github.com/yaalalabs/agent-kernel/tree/develop/.github"
                target="_blank"
                rel="noopener noreferrer"
              >
                .github/
              </a>
              .
            </p>
          </>
        )}
      </main>
    </Layout>
  );
}
