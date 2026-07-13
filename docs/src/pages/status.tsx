import React, { useEffect, useMemo, useState } from "react";
import Layout from "@theme/Layout";
import { load as parseYaml } from "js-yaml";
import type { IconType } from "react-icons";
import {
  MdDataObject,
  MdExtension,
  MdHub,
  MdMemory,
  MdScience,
  MdTerminal,
  MdWebhook,
} from "react-icons/md";
import styles from "./status.module.css";

/**
 * Integration Status — public dashboard showing red/green health for every
 * integration Agent Kernel supports, derived from the latest GitHub Actions
 * runs on the source branch.
 *
 * Every test job publishes its own tile to the orphan `status-data` branch as
 * soon as it finishes (see .github/scripts/publish_integration_status.py):
 *   status/<workflow>.json   - one entry per tile, with per-tile run metadata
 *   history/<workflow>.jsonl - one line per superseded tile status
 *
 * The full tile catalog is read from the test config YAMLs on the source
 * branch, so every configured integration is shown even before it has run
 * (status "no data"). See docs/specs/integration-status.md.
 */

const REPO = "yaalalabs/agent-kernel";

// TODO: change to "develop" when the feature branch is merged.
const SOURCE_BRANCH = "feature/health_dashboard";

const DATA_BASE = `https://raw.githubusercontent.com/${REPO}/status-data`;
const CONFIG_BASE = `https://raw.githubusercontent.com/${REPO}/${SOURCE_BRANCH}/.github`;

const WORKFLOW_KEYS = ["test", "integration-test", "integration-test-weekly"];

const WORKFLOW_META: Record<
  string,
  { title: string; file: string; config: string; tiers: string[]; includeBase: boolean; cadenceHours: number | null }
> = {
  test: {
    title: "Core tests (per commit)",
    file: "test.yaml",
    config: "test-config.yaml",
    tiers: ["e2e"],
    includeBase: false,
    cadenceHours: null,
  },
  "integration-test": {
    title: "Messaging integrations (nightly)",
    file: "integration-test.yaml",
    config: "integration-test-config.yaml",
    tiers: ["nightly"],
    includeBase: true,
    cadenceHours: 48,
  },
  "integration-test-weekly": {
    title: "Cloud deployments (weekly)",
    file: "integration-test-weekly.yaml",
    config: "integration-test-config.yaml",
    tiers: ["weekly"],
    includeBase: true,
    cadenceHours: 192,
  },
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

type TileStatus = "pass" | "fail" | "skipped" | "unknown" | "nodata";

interface StatusResult {
  path: string;
  type: string;
  category: string;
  label: string;
  description: string | null;
  status: TileStatus;
  run_id: number;
  run_url: string;
  job_url: string | null;
  completed_at: string;
}

interface StatusDoc {
  workflow: string;
  workflow_name: string;
  branch: string;
  commit: string;
  updated_at: string;
  expected_cadence_hours: number | null;
  results: StatusResult[];
}

interface HistoryEvent {
  key: string;
  status: TileStatus;
  run_id: number;
  run_url: string;
  completed_at: string;
}

interface CatalogTile {
  path: string;
  type: string;
  category: string;
  label: string;
  description: string | null;
}

interface Tile {
  path: string;
  type: string;
  category: string;
  label: string;
  description: string | null;
  status: TileStatus;
  href: string;
  completedAt: string | null;
  stale: boolean;
  history: { status: TileStatus; completedAt: string }[];
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

function isStaleTimestamp(iso: string | null, cadenceHours: number | null): boolean {
  if (!iso || cadenceHours == null) return false;
  return (Date.now() - new Date(iso).getTime()) / 3_600_000 > cadenceHours;
}

/* ─── Catalog from the config YAMLs (mirrors .github/scripts/dashboard_config.py) ── */

const DEFAULT_CATEGORY_BY_TYPE: Record<string, string> = {
  cli: "Core & Examples",
  api: "Core & Examples",
  memory: "Core & Examples",
  containerized: "Core & Examples",
  "aws-serverless": "AWS Serverless",
  "aws-containerized": "AWS Containerized",
  "azure-serverless": "Azure Serverless",
  "azure-containerized": "Azure Containerized",
  "gcp-serverless": "GCP Serverless",
  "gcp-containerized": "GCP Containerized",
};

// Non-matrix tiles published by the Test workflow's unit/script test jobs
// (mirrors SYNTHETIC_TILES in publish_integration_status.py)
const SYNTHETIC_TILES: CatalogTile[] = [
  {
    path: "ak-py",
    type: "unit",
    category: "Core & Frameworks",
    label: "ak-py unit tests",
    description: "ak-py library unit test suite",
  },
  {
    path: "scripts",
    type: "scripts",
    category: "Core & Frameworks",
    label: "Utility script tests",
    description: "Repository maintenance script tests",
  },
];

function defaultLabel(path: string): string {
  const segments = path.split("/").filter((s) => s && s !== "examples");
  return segments.slice(-2).join(" / ") || path;
}

function resolveCatalogTiles(test: any): CatalogTile[] {
  if (!test?.path || test.dashboard === "hidden") return [];
  const base = {
    path: test.path,
    type: test.type ?? "",
  };
  const fallback = {
    category: DEFAULT_CATEGORY_BY_TYPE[test.type] ?? "Other",
    label: defaultLabel(test.path),
  };
  const entries = test.dashboard
    ? Array.isArray(test.dashboard)
      ? test.dashboard
      : [test.dashboard]
    : [{}];
  return entries.map((entry: any) => ({
    ...base,
    category: entry?.category ?? fallback.category,
    label: entry?.label ?? fallback.label,
    description: entry?.description ?? null,
  }));
}

function buildCatalog(
  workflowKey: string,
  configs: Record<string, any>
): CatalogTile[] {
  const meta = WORKFLOW_META[workflowKey];
  const config = configs[meta.config];
  const tiles: CatalogTile[] = [];
  if (config) {
    if (meta.includeBase) {
      for (const test of config.deployment_base ?? []) {
        tiles.push(...resolveCatalogTiles(test));
      }
    }
    for (const tier of meta.tiers) {
      for (const test of config[tier]?.tests ?? []) {
        tiles.push(...resolveCatalogTiles(test));
      }
    }
  }
  if (workflowKey === "test") {
    tiles.push(...SYNTHETIC_TILES);
  }
  return tiles;
}

/* ─── Merge catalog + published data into tiles ───────────────────────────── */

function buildTiles(
  workflowKey: string,
  catalog: CatalogTile[],
  doc: StatusDoc | null,
  events: HistoryEvent[]
): Tile[] {
  const meta = WORKFLOW_META[workflowKey];
  const workflowUrl = `https://github.com/${REPO}/actions/workflows/${meta.file}`;
  const published = new Map<string, StatusResult>();
  for (const result of doc?.results ?? []) {
    published.set(tileKey(result.category, result.label), result);
  }

  const merged = new Map<string, CatalogTile>();
  for (const tile of catalog) {
    merged.set(tileKey(tile.category, tile.label), tile);
  }
  // Published tiles no longer in the catalog still render (they have data)
  for (const [key, result] of published) {
    if (!merged.has(key)) {
      merged.set(key, {
        path: result.path,
        type: result.type,
        category: result.category,
        label: result.label,
        description: result.description,
      });
    }
  }

  return [...merged.entries()].map(([key, catalogTile]) => {
    const result = published.get(key);
    const past = events
      .filter((event) => event.key === key)
      .map((event) => ({ status: event.status, completedAt: event.completed_at }));

    if (!result) {
      return {
        ...catalogTile,
        status: "nodata" as TileStatus,
        href: workflowUrl,
        completedAt: null,
        stale: false,
        history: past.slice(-HISTORY_STRIP_LENGTH),
        failingSince: null,
      };
    }

    const strip = [
      ...past,
      { status: result.status, completedAt: result.completed_at },
    ].slice(-HISTORY_STRIP_LENGTH);

    let failingSince: string | null = null;
    if (result.status === "fail") {
      failingSince = result.completed_at;
      for (let i = past.length - 1; i >= 0; i--) {
        if (past[i].status !== "fail") break;
        failingSince = past[i].completedAt;
      }
    }

    return {
      ...catalogTile,
      description: result.description ?? catalogTile.description,
      status: result.status,
      href: result.job_url ?? result.run_url ?? workflowUrl,
      completedAt: result.completed_at,
      stale: isStaleTimestamp(result.completed_at, meta.cadenceHours),
      history: strip,
      failingSince,
    };
  });
}

/* ─── Mock data for styling work (?mock=1 only) ───────────────────────────── */

function mockState() {
  const now = new Date().toISOString();
  const docs: Record<string, StatusDoc> = {
    test: {
      workflow: "test",
      workflow_name: "Test",
      branch: SOURCE_BRANCH,
      commit: "mock0000",
      updated_at: now,
      expected_cadence_hours: null,
      results: [
        {
          path: "examples/cli/openai",
          type: "cli",
          category: "Core & Frameworks",
          label: "OpenAI (CLI)",
          description: "Mock data",
          status: "pass",
          run_id: 1,
          run_url: `https://github.com/${REPO}/actions`,
          job_url: null,
          completed_at: now,
        },
        {
          path: "examples/cli/crewai",
          type: "cli",
          category: "Core & Frameworks",
          label: "CrewAI (CLI)",
          description: "Mock data",
          status: "fail",
          run_id: 1,
          run_url: `https://github.com/${REPO}/actions`,
          job_url: null,
          completed_at: now,
        },
      ],
    },
  };
  const configs: Record<string, any> = {
    "test-config.yaml": {
      e2e: {
        tests: [
          { type: "cli", path: "examples/cli/openai", dashboard: { category: "Core & Frameworks", label: "OpenAI (CLI)" } },
          { type: "cli", path: "examples/cli/crewai", dashboard: { category: "Core & Frameworks", label: "CrewAI (CLI)" } },
          { type: "api", path: "examples/api/hooks", dashboard: { category: "API Features", label: "Hooks" } },
          { type: "api", path: "examples/api/slack", dashboard: { category: "Messaging Integrations", label: "Slack" } },
        ],
      },
    },
    "integration-test-config.yaml": { nightly: { tests: [] }, weekly: { tests: [] } },
  };
  return { docs, configs, histories: {} as Record<string, HistoryEvent[]> };
}

function shouldMock(): boolean {
  if (typeof window === "undefined") return false;
  return window.location.search.includes("mock=1");
}

/* ─── Tile logos ──────────────────────────────────────────────────────────── */

/** Brand logos live in docs/static/img/integrations; generic capabilities use
 * the site's Material icon set. Rules are matched against "path label" in
 * order, so storage/platform brands win over the framework driving the test. */
const LOGO_RULES: { match: RegExp; img?: string; icon?: IconType }[] = [
  { match: /slack/, img: "slack-logo.png" },
  { match: /telegram/, img: "telegram-logo.png" },
  { match: /messenger/, img: "messenger-logo.png" },
  { match: /whatsapp/, img: "whatsapp-logo.png" },
  { match: /instagram/, img: "instagram-logo.png" },
  { match: /gmail/, img: "gmail-logo.png" },
  { match: /bedrock/, img: "bedrock.png" },
  { match: /chromadb/, img: "chromadb.png" },
  { match: /redis/, img: "redis.svg" },
  { match: /dynamodb/, img: "dynamodb.svg" },
  { match: /cosmos/, img: "cosmosdb.svg" },
  { match: /firestore/, img: "firestore.svg" },
  { match: /mcp/, img: "mcp.svg" },
  { match: /a2a/, img: "a2a.png" },
  { match: /crewai/, img: "crewai.png" },
  { match: /langgraph/, img: "langgraph.png" },
  { match: /adk/, img: "googleADK.png" },
  { match: /openai/, img: "chatgpt.png" },
  { match: /key-value/, icon: MdMemory },
  { match: /hooks/, icon: MdWebhook },
  { match: /structured/, icon: MdDataObject },
  { match: /unit tests/, icon: MdScience },
  { match: /script/, icon: MdTerminal },
  { match: /multi/, icon: MdHub },
];

const TYPE_LOGO_FALLBACK: Record<string, string> = {
  "aws-serverless": "aws.svg",
  "aws-containerized": "aws.svg",
  "azure-serverless": "azure.svg",
  "azure-containerized": "azure.svg",
  "gcp-serverless": "gcp.svg",
  "gcp-containerized": "gcp.svg",
};

function TileLogo({ tile }: { tile: Tile }) {
  const haystack = `${tile.path} ${tile.label}`.toLowerCase();
  const rule = LOGO_RULES.find((candidate) => candidate.match.test(haystack));
  const img = rule?.img ?? TYPE_LOGO_FALLBACK[tile.type];
  if (img) {
    return (
      <img
        className={styles.tileLogo}
        src={`/img/integrations/${img}`}
        alt=""
        loading="lazy"
      />
    );
  }
  const Icon = rule?.icon ?? MdExtension;
  return <Icon className={styles.tileIconLogo} aria-hidden="true" />;
}

/* ─── Rendering ───────────────────────────────────────────────────────────── */

const STATUS_DOT: Record<TileStatus, string> = {
  pass: styles.dotPass,
  fail: styles.dotFail,
  skipped: styles.dotSkipped,
  unknown: styles.dotUnknown,
  nodata: styles.dotNodata,
};

const STATUS_CELL: Record<TileStatus, string> = {
  pass: styles.historyPass,
  fail: styles.historyFail,
  skipped: styles.historySkipped,
  unknown: styles.historyUnknown,
  nodata: styles.historyUnknown,
};

function TileCard({ tile }: { tile: Tile }) {
  const tooltip = [
    tile.description,
    `Example: ${tile.path}`,
    tile.status === "nodata"
      ? "No published runs yet"
      : `Status: ${tile.status}${tile.stale ? " (stale)" : ""}`,
    tile.completedAt
      ? `Last run: ${new Date(tile.completedAt).toLocaleString()}`
      : null,
    "Click to open the GitHub Actions run",
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <a
      className={styles.tile}
      href={tile.href}
      target="_blank"
      rel="noopener noreferrer"
      title={tooltip}
    >
      <div className={styles.tileHeader}>
        <span className={`${styles.dot} ${STATUS_DOT[tile.status]}`} />
        <TileLogo tile={tile} />
        <span className={styles.tileLabel}>{tile.label}</span>
        {tile.stale && <span className={styles.staleBadge}>stale</span>}
        <span className={styles.externalHint} aria-hidden="true">
          ↗
        </span>
      </div>
      <div className={styles.tileMeta}>
        {tile.status === "nodata" ? (
          <span>no runs yet</span>
        ) : tile.status === "fail" && tile.failingSince ? (
          <span className={styles.failingSince}>
            failing since {new Date(tile.failingSince).toLocaleDateString()}
          </span>
        ) : (
          <span>updated {tile.completedAt ? relativeTime(tile.completedAt) : "n/a"}</span>
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
                  ? `${cell.status} · ${new Date(cell.completedAt).toLocaleString()}`
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
  const [docs, setDocs] = useState<Record<string, StatusDoc>>({});
  const [histories, setHistories] = useState<Record<string, HistoryEvent[]>>({});
  const [configs, setConfigs] = useState<Record<string, any>>({});
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function fetchText(url: string): Promise<string> {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`${url}: ${response.status}`);
      return response.text();
    }

    async function load() {
      if (shouldMock()) {
        const mock = mockState();
        setDocs(mock.docs);
        setConfigs(mock.configs);
        setHistories(mock.histories);
        setLoaded(true);
        return;
      }

      const configNames = ["test-config.yaml", "integration-test-config.yaml"];
      const [statusResults, historyResults, configResults] = await Promise.all([
        Promise.allSettled(
          WORKFLOW_KEYS.map(async (key) => ({
            key,
            doc: JSON.parse(await fetchText(`${DATA_BASE}/status/${key}.json`)) as StatusDoc,
          }))
        ),
        Promise.allSettled(
          WORKFLOW_KEYS.map(async (key) => ({
            key,
            events: (await fetchText(`${DATA_BASE}/history/${key}.jsonl`))
              .split("\n")
              .filter((line) => line.trim())
              .map((line) => {
                try {
                  return JSON.parse(line) as HistoryEvent;
                } catch {
                  return null;
                }
              })
              .filter((event): event is HistoryEvent => event !== null),
          }))
        ),
        Promise.allSettled(
          configNames.map(async (name) => ({
            name,
            config: parseYaml(await fetchText(`${CONFIG_BASE}/${name}`)),
          }))
        ),
      ]);
      if (cancelled) return;

      const loadedDocs: Record<string, StatusDoc> = {};
      for (const result of statusResults) {
        if (result.status === "fulfilled") loadedDocs[result.value.key] = result.value.doc;
      }
      const loadedHistories: Record<string, HistoryEvent[]> = {};
      for (const result of historyResults) {
        if (result.status === "fulfilled") loadedHistories[result.value.key] = result.value.events;
      }
      const loadedConfigs: Record<string, any> = {};
      for (const result of configResults) {
        if (result.status === "fulfilled") loadedConfigs[result.value.name] = result.value.config;
      }

      setDocs(loadedDocs);
      setHistories(loadedHistories);
      setConfigs(loadedConfigs);
      setLoaded(true);
    }

    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const tiles = useMemo(() => {
    if (!loaded) return [];
    return WORKFLOW_KEYS.flatMap((key) =>
      buildTiles(key, buildCatalog(key, configs), docs[key] ?? null, histories[key] ?? [])
    );
  }, [loaded, docs, histories, configs]);

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
    return [...known, ...extra].map((category) => {
      const categoryTiles = (grouped.get(category) ?? []).sort((a, b) =>
        a.label.localeCompare(b.label)
      );
      return {
        category,
        tiles: categoryTiles,
        passing: categoryTiles.filter((tile) => tile.status === "pass").length,
        failing: categoryTiles.filter((tile) => tile.status === "fail").length,
      };
    });
  }, [tiles]);

  const summary = useMemo(() => {
    const passing = tiles.filter((tile) => tile.status === "pass").length;
    const failing = tiles.filter((tile) => tile.status === "fail").length;
    const nodata = tiles.filter((tile) => tile.status === "nodata").length;
    const stale = tiles.filter((tile) => tile.stale).length;
    const other = tiles.length - passing - failing - nodata;
    return { passing, failing, nodata, stale, other };
  }, [tiles]);

  const nothingAvailable =
    loaded && tiles.length === 0;

  return (
    <Layout
      title="Integration Status"
      description="Live health of Agent Kernel's framework, cloud, memory, and messaging integrations, based on the latest test runs."
    >
      <main className={styles.statusPage}>
        <header className={styles.hero}>
          <div className={styles.heroOrb} />
          <div className={styles.badge}>
            <span className={styles.badgeStar}>✦</span>
            Live Test Results
          </div>
          <h1 className={styles.heroTitle}>Integration Status</h1>
          <p className={styles.heroSubtitle}>
            Live health of every integration Agent Kernel supports: agent
            frameworks, cloud deployment variants, memory and knowledge
            backends, and messaging platforms. Statuses come from the latest
            test pipeline runs on the <code>{SOURCE_BRANCH}</code> branch, and
            every tile links to the GitHub Actions run that produced it.
          </p>

          {loaded && tiles.length > 0 && (
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
                {summary.nodata > 0 && (
                  <span className={styles.summaryChip}>
                    <span className={`${styles.dot} ${styles.dotNodata}`} />
                    {summary.nodata} no data
                  </span>
                )}
                {summary.other > 0 && (
                  <span className={styles.summaryChip}>
                    <span className={`${styles.dot} ${styles.dotSkipped}`} />
                    {summary.other} skipped
                  </span>
                )}
                {summary.stale > 0 && (
                  <span className={styles.summaryChip}>
                    <span className={styles.staleBadge}>stale</span>
                    {summary.stale} stale
                  </span>
                )}
              </div>
              <div className={styles.sourcesRow}>
                {WORKFLOW_KEYS.map((key) => {
                  const meta = WORKFLOW_META[key];
                  const doc = docs[key];
                  const href = `https://github.com/${REPO}/actions/workflows/${meta.file}`;
                  return (
                    <a
                      key={key}
                      className={styles.sourceCard}
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      title="Open the workflow's GitHub Actions runs"
                    >
                      <span className={styles.sourceTitle}>
                        {meta.title}
                        <span className={styles.sourceArrow} aria-hidden="true">
                          →
                        </span>
                      </span>
                      <span className={styles.sourceMeta}>
                        {doc ? (
                          <>
                            last update {relativeTime(doc.updated_at)} · commit{" "}
                            <code>{doc.commit}</code>
                            {isStaleTimestamp(doc.updated_at, meta.cadenceHours) && (
                              <span className={styles.staleSource}> · stale</span>
                            )}
                          </>
                        ) : (
                          "no runs published yet"
                        )}
                      </span>
                    </a>
                  );
                })}
              </div>
            </>
          )}
        </header>

        <div className={styles.content}>
          {nothingAvailable && (
            <div className={styles.notice}>
              Status data is not available right now. It is published by the
              test workflows on the <code>{SOURCE_BRANCH}</code> branch. You can
              check the{" "}
              <a
                href={`https://github.com/${REPO}/actions`}
                target="_blank"
                rel="noopener noreferrer"
              >
                GitHub Actions runs
              </a>{" "}
              directly.
            </div>
          )}

          {!loaded && (
            <div className={styles.grid} style={{ marginTop: "3rem" }}>
              {Array.from({ length: 12 }).map((_, index) => (
                <div key={index} className={styles.skeleton} />
              ))}
            </div>
          )}

          {categories.map(({ category, tiles: categoryTiles, passing, failing }) => (
            <section key={category} className={styles.section}>
              <div className={styles.sectionGlow} />
              <h2 className={styles.sectionTitle}>{category}</h2>
              <span className={styles.sectionCounts}>
                {passing} passing
                {failing > 0 && (
                  <span className={styles.sectionCountFail}>
                    {" "}
                    · {failing} failing
                  </span>
                )}
              </span>
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

          {loaded && tiles.length > 0 && (
            <>
              <div className={styles.legend}>
                <div className={styles.sectionGlow} />
                <span className={styles.legendItem}>
                  <span className={`${styles.dot} ${styles.dotPass}`} /> passing
                </span>
                <span className={styles.legendItem}>
                  <span className={`${styles.dot} ${styles.dotFail}`} /> failing
                </span>
                <span className={styles.legendItem}>
                  <span className={`${styles.dot} ${styles.dotSkipped}`} />{" "}
                  skipped
                </span>
                <span className={styles.legendItem}>
                  <span className={`${styles.dot} ${styles.dotNodata}`} /> no
                  runs yet
                </span>
                <span className={styles.legendItem}>
                  <span className={styles.staleBadge}>stale</span> last run is
                  older than its expected cadence
                </span>
              </div>
              <p className={styles.footerNote}>
                Each tile's strip shows its most recent runs, oldest to newest.
                Every test job publishes its own status the moment it finishes.
                Tests are categorized via the config files in{" "}
                <a
                  href={`https://github.com/${REPO}/tree/${SOURCE_BRANCH}/.github`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  .github/
                </a>
                .
              </p>
            </>
          )}
        </div>
      </main>
    </Layout>
  );
}
