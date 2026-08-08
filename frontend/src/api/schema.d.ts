/**
 * GENERATED FILE — do not edit.
 * Run `pnpm gen:api`. See src/api/README.md and docs/architecture.md §7.
 */
export interface paths {
    readonly "/api/dashboard": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Dashboard */
        readonly get: operations["get_dashboard_api_dashboard_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/dashboard/system": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get System Snapshot */
        readonly get: operations["get_system_snapshot_api_dashboard_system_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/graphs": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /**
         * Create Graph
         * @description Persist a proposed DAG: one session, its nodes, and its edges.
         *
         *     422 with the typed defects when the proposal is not a DAG — a cycle, a
         *     ``depends_on`` naming a slug that is not in the body, a duplicate name. The
         *     validation happens before the first row, so a rejected proposal leaves
         *     nothing behind (the alternative, half a graph on disk, is worse than none).
         */
        readonly post: operations["create_graph_api_graphs_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/graphs/plan": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /**
         * Plan Graph
         * @description Plan and persist an objective as a proposal; never approve or run it.
         */
        readonly post: operations["plan_graph_api_graphs_plan_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/graphs/{session_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Graph */
        readonly get: operations["get_graph_api_graphs__session_id__get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/graphs/{session_id}/approve": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Approve Graph */
        readonly post: operations["approve_graph_api_graphs__session_id__approve_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/graphs/{session_id}/runs": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Run Graph */
        readonly post: operations["run_graph_api_graphs__session_id__runs_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Sessions */
        readonly get: operations["list_sessions_api_sessions_get"];
        readonly put?: never;
        /** Create Session */
        readonly post: operations["create_session_api_sessions_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Session */
        readonly get: operations["get_session_api_sessions__session_id__get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/approve": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Approve */
        readonly post: operations["approve_api_sessions__session_id__approve_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/diff": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Diff */
        readonly get: operations["get_diff_api_sessions__session_id__diff_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/kill": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Kill Run */
        readonly post: operations["kill_run_api_sessions__session_id__kill_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/node": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Node */
        readonly get: operations["get_node_api_sessions__session_id__node_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Nodes */
        readonly get: operations["list_nodes_api_sessions__session_id__nodes_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Node */
        readonly get: operations["get_node_api_sessions__session_id__nodes__node_id__get"];
        /** Update Node */
        readonly put: operations["update_node_api_sessions__session_id__nodes__node_id__put"];
        readonly post?: never;
        /** Delete Node */
        readonly delete: operations["delete_node_api_sessions__session_id__nodes__node_id__delete"];
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/acceptance": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /**
         * List Acceptance Results
         * @description The per-criterion checklist, oldest attempt first.
         *
         *     Without ``attempt`` this is the whole history, which is what a drawer
         *     showing "attempt 2 fixed the criterion attempt 1 failed" needs. With it,
         *     one attempt — the panel `design.md` §8 specifies for ``awaiting_review``.
         */
        readonly get: operations["list_acceptance_results_api_sessions__session_id__nodes__node_id__acceptance_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/acceptance/{attempt}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        /** Resolve Acceptance Results */
        readonly patch: operations["resolve_acceptance_results_api_sessions__session_id__nodes__node_id__acceptance__attempt__patch"];
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/approve": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /**
         * Approve Node
         * @description The human gate's yes: record the verdict, then merge (invariant 6).
         */
        readonly post: operations["approve_node_api_sessions__session_id__nodes__node_id__approve_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/dependencies/{depends_on_id}": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        /** Add Dependency */
        readonly put: operations["add_dependency_api_sessions__session_id__nodes__node_id__dependencies__depends_on_id__put"];
        readonly post?: never;
        /** Remove Dependency */
        readonly delete: operations["remove_dependency_api_sessions__session_id__nodes__node_id__dependencies__depends_on_id__delete"];
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/diff": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Diff */
        readonly get: operations["get_diff_api_sessions__session_id__nodes__node_id__diff_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/kill": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Kill Node */
        readonly post: operations["kill_node_api_sessions__session_id__nodes__node_id__kill_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/reject": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /**
         * Reject Node
         * @description Persist the rejection, then let the graph scheduler own the retry.
         */
        readonly post: operations["reject_node_api_sessions__session_id__nodes__node_id__reject_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/retry": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Retry Node */
        readonly post: operations["retry_node_api_sessions__session_id__nodes__node_id__retry_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/reviews": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Reviews */
        readonly get: operations["list_reviews_api_sessions__session_id__nodes__node_id__reviews_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/runs": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Runs */
        readonly get: operations["list_runs_api_sessions__session_id__nodes__node_id__runs_get"];
        readonly put?: never;
        /**
         * Run Node
         * @description Run one already-materialized node to its terminal or gated state.
         *
         *     Synchronous, exactly as Phase 1's ``POST /runs`` is: the response *is* the
         *     outcome. See the module note on :func:`reject_node` for why that is a
         *     deliberate cost here and a reported problem there.
         */
        readonly post: operations["run_node_api_sessions__session_id__nodes__node_id__runs_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/runs/{run_id}/events": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Run Events */
        readonly get: operations["list_run_events_api_sessions__session_id__nodes__node_id__runs__run_id__events_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/nodes/{node_id}/runs/{run_id}/summary": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Run Summary */
        readonly get: operations["get_run_summary_api_sessions__session_id__nodes__node_id__runs__run_id__summary_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/retry": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly get?: never;
        readonly put?: never;
        /** Retry Run */
        readonly post: operations["retry_run_api_sessions__session_id__retry_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/runs": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Runs */
        readonly get: operations["list_runs_api_sessions__session_id__runs_get"];
        readonly put?: never;
        /** Start Run */
        readonly post: operations["start_run_api_sessions__session_id__runs_post"];
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/runs/{run_id}/events": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** List Run Events */
        readonly get: operations["list_run_events_api_sessions__session_id__runs__run_id__events_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/api/sessions/{session_id}/runs/{run_id}/summary": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Get Run Summary */
        readonly get: operations["get_run_summary_api_sessions__session_id__runs__run_id__summary_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
    readonly "/health": {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        /** Health */
        readonly get: operations["health_health_get"];
        readonly put?: never;
        readonly post?: never;
        readonly delete?: never;
        readonly options?: never;
        readonly head?: never;
        readonly patch?: never;
        readonly trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /**
         * AcceptanceResultResponse
         * @description One criterion of `design.md` §8's ``awaiting_review`` checklist.
         */
        readonly AcceptanceResultResponse: {
            /** Attempt */
            readonly attempt: number;
            /** Created Ms */
            readonly created_ms: number;
            /** Criterion */
            readonly criterion: string;
            /** Node Id */
            readonly node_id: string;
            readonly outcome: components["schemas"]["CriterionOutcome"];
            /** Position */
            readonly position: number;
            /** Updated Ms */
            readonly updated_ms: number;
        };
        /** ActiveSessionMetricResponse */
        readonly ActiveSessionMetricResponse: {
            /** Blocked Nodes */
            readonly blocked_nodes: number;
            /** Completed Nodes */
            readonly completed_nodes: number;
            /** Created Ms */
            readonly created_ms: number;
            /** Elapsed Ms */
            readonly elapsed_ms: number;
            /** Harnesses */
            readonly harnesses: readonly string[];
            /** Id */
            readonly id: string;
            readonly status: components["schemas"]["SessionStatus"];
            /** Title */
            readonly title: string;
            /** Total Nodes */
            readonly total_nodes: number;
            readonly usage: components["schemas"]["MetricUsageResponse"];
        };
        /** AgentProcessMetricResponse */
        readonly AgentProcessMetricResponse: {
            /** Cpu Percent */
            readonly cpu_percent: number;
            /** Harness */
            readonly harness: string;
            /** Node Id */
            readonly node_id: string;
            /** Pid */
            readonly pid: number;
            /** Process Count */
            readonly process_count: number;
            /** Rss Bytes */
            readonly rss_bytes: number;
            /** Uptime Ms */
            readonly uptime_ms: number;
        };
        /**
         * CreateGraphRequest
         * @description A whole proposed graph, persisted in one call.
         *
         *     One call and not "create session, then POST each node": a half-written
         *     graph is a graph, and a scheduler reading one would happily start the
         *     fragment it can see. ``create_graph`` validates the DAG before the first row
         *     (invariant 6 — this is a proposal, and persisting it starts nothing).
         */
        readonly CreateGraphRequest: {
            /**
             * Auto Merge
             * @default false
             */
            readonly auto_merge: boolean;
            /**
             * Base Ref
             * @default HEAD
             */
            readonly base_ref: string;
            /** Nodes */
            readonly nodes: readonly components["schemas"]["PlannedNodeRequest"][];
            /**
             * Repo Path
             * Format: path
             */
            readonly repo_path: string;
            /** Title */
            readonly title?: string | null;
        };
        /** CreateSessionRequest */
        readonly CreateSessionRequest: {
            /**
             * Acceptance Criteria
             * @default []
             */
            readonly acceptance_criteria: readonly string[];
            /**
             * Auto Merge
             * @default false
             */
            readonly auto_merge: boolean;
            /**
             * Base Ref
             * @default HEAD
             */
            readonly base_ref: string;
            /** Harness */
            readonly harness: string;
            /** Model */
            readonly model?: string | null;
            /** Prompt */
            readonly prompt: string;
            /**
             * Repo Path
             * Format: path
             */
            readonly repo_path: string;
            /** Title */
            readonly title?: string | null;
        };
        /** CreatedGraphResponse */
        readonly CreatedGraphResponse: {
            /** Ids By Name */
            readonly ids_by_name: {
                readonly [key: string]: string;
            };
            /** Nodes */
            readonly nodes: readonly components["schemas"]["NodeResponse"][];
            readonly session: components["schemas"]["SessionResponse"];
        };
        /** CreatedSessionResponse */
        readonly CreatedSessionResponse: {
            readonly node: components["schemas"]["NodeResponse"];
            readonly session: components["schemas"]["SessionResponse"];
        };
        /**
         * CriterionOutcome
         * @description What a human decided about one acceptance criterion.
         *
         *     `design.md` §9 is explicit that ``check_acceptance`` does **not** evaluate
         *     the criteria: §8 emits them as prose ("pytest tests/test_auth.py passes"
         *     *describes* a command, it is not one) and guessing which strings are
         *     runnable would be a heuristic that silently passes a criterion it failed to
         *     understand.
         *
         *     ``UNEVALUATED`` is a member and not the absence of a row, which is the one
         *     decision here worth arguing. An absent row cannot distinguish three
         *     different things: the reviewer has not reached this criterion yet, the
         *     criterion did not exist when the run happened (a node's criteria are
         *     authored and editable), and nobody was ever going to look because
         *     ``auto_merge`` was on. Recording the snapshot with an explicit
         *     ``unevaluated`` makes `design.md` §9's stated limitation — an unattended
         *     graph merges on the harness's own verdict — visible **in the data** instead
         *     of inferable from a missing join.
         * @enum {string}
         */
        readonly CriterionOutcome: "unevaluated" | "pass" | "fail";
        /**
         * DashboardPeriod
         * @enum {string}
         */
        readonly DashboardPeriod: "today" | "7d" | "30d";
        /** DashboardResponse */
        readonly DashboardResponse: {
            /** Active Session Count */
            readonly active_session_count: number;
            /** Active Sessions */
            readonly active_sessions: readonly components["schemas"]["ActiveSessionMetricResponse"][];
            /** Blocked Node Count */
            readonly blocked_node_count: number;
            /** By Harness */
            readonly by_harness: readonly components["schemas"]["MetricUsageResponse"][];
            /** By Model */
            readonly by_model: readonly components["schemas"]["MetricUsageResponse"][];
            /** Event Feed */
            readonly event_feed: readonly components["schemas"]["DashboardTransitionResponse"][];
            /** Generated Ms */
            readonly generated_ms: number;
            /** Node Completion Rate */
            readonly node_completion_rate: number | null;
            readonly period: components["schemas"]["DashboardPeriod"];
            /** Running Node Count */
            readonly running_node_count: number;
            /** Since Ms */
            readonly since_ms: number;
            readonly usage: components["schemas"]["MetricUsageResponse"];
        };
        /** DashboardTransitionResponse */
        readonly DashboardTransitionResponse: {
            /** Id */
            readonly id: number;
            /** Node Id */
            readonly node_id: string;
            /** Node Name */
            readonly node_name: string;
            /** Session Id */
            readonly session_id: string;
            /** Session Title */
            readonly session_title: string;
            readonly status: components["schemas"]["NodeStatus"];
            /** Ts */
            readonly ts: number;
        };
        /** DiffResponse */
        readonly DiffResponse: {
            /** Patch */
            readonly patch: string;
        };
        /** GraphResponse */
        readonly GraphResponse: {
            /** Edges */
            readonly edges: readonly components["schemas"]["NodeDependencyResponse"][];
            /** Nodes */
            readonly nodes: readonly components["schemas"]["NodeResponse"][];
            readonly session: components["schemas"]["SessionResponse"];
        };
        /** GraphRunResponse */
        readonly GraphRunResponse: {
            /** Scheduled */
            readonly scheduled: boolean;
            /** Session Id */
            readonly session_id: string;
        };
        /** HTTPValidationError */
        readonly HTTPValidationError: {
            /** Detail */
            readonly detail?: readonly components["schemas"]["ValidationError"][];
        };
        /** HealthResponse */
        readonly HealthResponse: {
            /**
             * Status
             * @default ok
             * @constant
             */
            readonly status: "ok";
        };
        /** MergeResponse */
        readonly MergeResponse: {
            /** Commit */
            readonly commit: string | null;
            /** Conflicts */
            readonly conflicts: readonly string[];
            /** Status */
            readonly status: string;
        };
        /** MetricUsageResponse */
        readonly MetricUsageResponse: {
            /** Cost Complete */
            readonly cost_complete: boolean;
            /** Estimated Equivalent Cost Usd */
            readonly estimated_equivalent_cost_usd: number | null;
            /** Key */
            readonly key: string;
            readonly tokens: components["schemas"]["TokenCountsResponse"];
        };
        /** NodeDependencyResponse */
        readonly NodeDependencyResponse: {
            /** Created Ms */
            readonly created_ms: number;
            /** Depends On Id */
            readonly depends_on_id: string;
            /** Node Id */
            readonly node_id: string;
            /** Session Id */
            readonly session_id: string;
        };
        /** NodeResponse */
        readonly NodeResponse: {
            /** Acceptance Criteria */
            readonly acceptance_criteria: readonly string[];
            /** Base Ref */
            readonly base_ref: string | null;
            /** Branch */
            readonly branch: string | null;
            /** Created Ms */
            readonly created_ms: number;
            /** Estimated Effort */
            readonly estimated_effort: string | null;
            /** Harness */
            readonly harness: string;
            /** Id */
            readonly id: string;
            /** Model */
            readonly model: string | null;
            /** Name */
            readonly name: string;
            /** Prompt */
            readonly prompt: string;
            /** Session Id */
            readonly session_id: string;
            readonly status: components["schemas"]["NodeStatus"];
            /** Touches */
            readonly touches: readonly string[];
            /** Updated Ms */
            readonly updated_ms: number;
            /** Worktree Path */
            readonly worktree_path: string | null;
        };
        /** NodeReviewResponse */
        readonly NodeReviewResponse: {
            /** Attempt */
            readonly attempt: number;
            readonly decision: components["schemas"]["ReviewDecision"];
            /** Feedback */
            readonly feedback: string | null;
            /** Node Id */
            readonly node_id: string;
            /** Reviewed Ms */
            readonly reviewed_ms: number;
        };
        /**
         * NodeStatus
         * @description One activity in the graph.
         *
         *     ``blocked`` is not a failure: it is a merge conflict or a permission gate
         *     waiting on a human, and it is reachable again. ``failed`` is the run's
         *     verdict. They are separate states because the operator's next action
         *     differs, which is also why `docs/design-system.md` §5 distinguishes them by
         *     icon rather than by hue.
         * @enum {string}
         */
        readonly NodeStatus: "pending" | "ready" | "running" | "awaiting_review" | "blocked" | "done" | "failed" | "skipped";
        /**
         * PlanGraphRequest
         * @description An objective that the planner turns into a gated graph proposal.
         */
        readonly PlanGraphRequest: {
            /**
             * Auto Merge
             * @default false
             */
            readonly auto_merge: boolean;
            /**
             * Base Ref
             * @default HEAD
             */
            readonly base_ref: string;
            /** Context */
            readonly context?: string | null;
            /** Objective */
            readonly objective: string;
            /**
             * Repo Path
             * Format: path
             */
            readonly repo_path: string;
        };
        /** PlannedGraphResponse */
        readonly PlannedGraphResponse: {
            /** Attempts */
            readonly attempts: number;
            /** Ids By Name */
            readonly ids_by_name: {
                readonly [key: string]: string;
            };
            /** Nodes */
            readonly nodes: readonly components["schemas"]["NodeResponse"][];
            readonly planner_usage: components["schemas"]["PlannerUsageResponse"];
            readonly session: components["schemas"]["SessionResponse"];
            /**
             * Status
             * @default proposal
             * @constant
             */
            readonly status: "proposal";
        };
        /**
         * PlannedNodeRequest
         * @description One activity of a proposed graph, keyed by name.
         *
         *     This is `design.md` §8's planner node schema on the wire, and the field
         *     names are deliberately the *stored* ones rather than the planner's:
         *     ``suggested_harness``/``suggested_model`` are a suggestion the operator has
         *     already answered by the time a graph is created, and §8 says the suggestion
         *     is not retained. C8's ``orchestrator/planner.py`` therefore maps its
         *     structured-output model onto this one, and a proposal reaches
         *     :meth:`~app.orchestrator.service.NodeRunService.create_graph` through the
         *     same call a hand-authored graph does.
         *
         *     ``depends_on`` names the *other nodes' ``name`` values*, not ids: the
         *     planner cannot know the ULIDs the database will allocate, and resolving
         *     slugs to ids happens in exactly one place (``create_graph``). An
         *     unresolvable name comes back as a typed ``unknown_dependency`` defect naming
         *     the slug, not as a 500.
         */
        readonly PlannedNodeRequest: {
            /**
             * Acceptance Criteria
             * @default []
             */
            readonly acceptance_criteria: readonly string[];
            /**
             * Depends On
             * @default []
             */
            readonly depends_on: readonly string[];
            /** Estimated Effort */
            readonly estimated_effort?: string | null;
            /** Harness */
            readonly harness: string;
            /** Model */
            readonly model?: string | null;
            /** Name */
            readonly name: string;
            /** Prompt */
            readonly prompt: string;
            /**
             * Touches
             * @default []
             */
            readonly touches: readonly string[];
        };
        /**
         * PlannerUsageResponse
         * @description Metered API usage, distinct from harness equivalent-cost estimates.
         */
        readonly PlannerUsageResponse: {
            /** Cost Usd */
            readonly cost_usd: number | null;
            /** Model */
            readonly model: string;
            /** Price Table Version */
            readonly price_table_version: number;
            /** Requests */
            readonly requests: number;
            readonly tokens: components["schemas"]["TokenCountsResponse"];
        };
        /** RejectRequest */
        readonly RejectRequest: {
            /** Feedback */
            readonly feedback: string;
            /** Outcomes */
            readonly outcomes?: {
                readonly [key: string]: components["schemas"]["CriterionOutcome"];
            };
        };
        /**
         * RetryRequest
         * @description A new attempt at a ``failed`` or ``blocked`` node.
         *
         *     ``feedback`` is optional here and mandatory on reject, and that asymmetry is
         *     the orchestrator's: retry is "try again", reject is a human overruling a
         *     finished attempt.
         */
        readonly RetryRequest: {
            /** Feedback */
            readonly feedback?: string | null;
        };
        /**
         * ReviewDecision
         * @description The human gate's two answers (`design.md` §8's ``awaiting_review`` row).
         *
         *     Approve merges into integration; reject opens a new attempt carrying the
         *     reviewer's feedback. There is no third answer: "not yet" is the absence of a
         *     :class:`NodeReview` row, which is what ``awaiting_review`` already says.
         * @enum {string}
         */
        readonly ReviewDecision: "approved" | "rejected";
        /**
         * ReviewOutcomesRequest
         * @description The reviewer's answers to the acceptance checklist, by position.
         *
         *     Partial on purpose: a position left out keeps whatever it had, which is
         *     ``unevaluated`` until somebody says otherwise. The orchestrator decides
         *     whether a ``fail`` disqualifies an approval — it does not, deliberately
         *     (see :meth:`~app.orchestrator.service.NodeRunService.approve_node`) — and
         *     this transport does not second-guess it.
         */
        readonly ReviewOutcomesRequest: {
            /** Outcomes */
            readonly outcomes?: {
                readonly [key: string]: components["schemas"]["CriterionOutcome"];
            };
        };
        /** RunOutcomeResponse */
        readonly RunOutcomeResponse: {
            /** Block Reason */
            readonly block_reason: string | null;
            /** Commit */
            readonly commit: string | null;
            /** Cost Complete */
            readonly cost_complete: boolean;
            /** Estimated Equivalent Cost Usd */
            readonly estimated_equivalent_cost_usd: number | null;
            /** Merged */
            readonly merged: boolean;
            /** Node Id */
            readonly node_id: string;
            readonly node_status: components["schemas"]["NodeStatus"];
            /** Permission Denials */
            readonly permission_denials: number;
            /** Run Id */
            readonly run_id: string;
            readonly run_status: components["schemas"]["RunState"];
            /** Session Id */
            readonly session_id: string;
            readonly tokens: components["schemas"]["TokenCountsResponse"];
            /** Trusted */
            readonly trusted: boolean;
        };
        /** RunResponse */
        readonly RunResponse: {
            /** Attempt */
            readonly attempt: number;
            /** Created Ms */
            readonly created_ms: number;
            /** Event Count */
            readonly event_count: number;
            /** Exit Code */
            readonly exit_code: number | null;
            /** Finished Ms */
            readonly finished_ms: number | null;
            /** Harness */
            readonly harness: string;
            /** Harness Session Id */
            readonly harness_session_id: string | null;
            /** Harness Version */
            readonly harness_version: string | null;
            /** Id */
            readonly id: string;
            /** Model */
            readonly model: string | null;
            /** Node Id */
            readonly node_id: string;
            /** Permission Denial Count */
            readonly permission_denial_count: number;
            /** Pid */
            readonly pid: number | null;
            /** Session Id */
            readonly session_id: string;
            /** Started Ms */
            readonly started_ms: number | null;
            readonly status: components["schemas"]["RunState"];
            /** Summary */
            readonly summary: string | null;
        };
        /**
         * RunState
         * @description One execution of a node.
         *
         *     ``RUNNING`` is the only non-terminal member and the only one with no
         *     counterpart in ``RunStatus``: a harness never *reports* "still running", it
         *     is the state of a row between :class:`~app.harnesses.events.RunStarted` and
         *     :class:`~app.harnesses.events.RunFinished`. A row still ``RUNNING`` after an
         *     orchestrator restart is an orphan, and the scheduler resolves it to
         *     ``INTERRUPTED`` rather than to a sixth state.
         * @enum {string}
         */
        readonly RunState: "running" | "success" | "failed" | "interrupted" | "budget_exceeded";
        /** RunSummaryResponse */
        readonly RunSummaryResponse: {
            /** Cost Complete */
            readonly cost_complete: boolean;
            /** Estimated Equivalent Cost Usd */
            readonly estimated_equivalent_cost_usd: number | null;
            /** Run Id */
            readonly run_id: string;
            readonly tokens: components["schemas"]["TokenCountsResponse"];
            /** Trusted */
            readonly trusted: boolean;
        };
        /** SessionResponse */
        readonly SessionResponse: {
            /** Auto Merge */
            readonly auto_merge: boolean;
            /** Created Ms */
            readonly created_ms: number;
            /** Id */
            readonly id: string;
            /** Integration Branch */
            readonly integration_branch: string;
            /**
             * Repo Path
             * Format: path
             */
            readonly repo_path: string;
            readonly status: components["schemas"]["SessionStatus"];
            /** Title */
            readonly title: string;
            /** Updated Ms */
            readonly updated_ms: number;
            /**
             * Workspace Root
             * Format: path
             */
            readonly workspace_root: string;
        };
        /**
         * SessionStatus
         * @description One planning conversation plus its graph.
         * @enum {string}
         */
        readonly SessionStatus: "planning" | "running" | "paused" | "done" | "failed";
        /** SystemSnapshotResponse */
        readonly SystemSnapshotResponse: {
            /** Cpu Per Core */
            readonly cpu_per_core: readonly number[];
            /** Cpu Percent */
            readonly cpu_percent: number;
            /** Disk Free Bytes */
            readonly disk_free_bytes: number;
            /** Disk Percent */
            readonly disk_percent: number;
            /** Disk Total Bytes */
            readonly disk_total_bytes: number;
            /** Disk Used Bytes */
            readonly disk_used_bytes: number;
            /** Memory Available Bytes */
            readonly memory_available_bytes: number;
            /** Memory Percent */
            readonly memory_percent: number;
            /** Memory Total Bytes */
            readonly memory_total_bytes: number;
            /** Memory Used Bytes */
            readonly memory_used_bytes: number;
            /** Processes */
            readonly processes: readonly components["schemas"]["AgentProcessMetricResponse"][];
            /** Swap Free Bytes */
            readonly swap_free_bytes: number;
            /** Swap Percent */
            readonly swap_percent: number;
            /** Swap Total Bytes */
            readonly swap_total_bytes: number;
            /** Swap Used Bytes */
            readonly swap_used_bytes: number;
            /** Ts */
            readonly ts: number;
        };
        /** TokenCountsResponse */
        readonly TokenCountsResponse: {
            /** Cache Read Tokens */
            readonly cache_read_tokens: number;
            /** Cache Write Tokens */
            readonly cache_write_tokens: number;
            /** Input Tokens */
            readonly input_tokens: number;
            /** Output Tokens */
            readonly output_tokens: number;
            /** Total Tokens */
            readonly total_tokens: number;
        };
        /**
         * UpdateNodeRequest
         * @description Complete replacement of a proposal node's authored fields.
         */
        readonly UpdateNodeRequest: {
            /**
             * Acceptance Criteria
             * @default []
             */
            readonly acceptance_criteria: readonly string[];
            /** Estimated Effort */
            readonly estimated_effort?: string | null;
            /** Harness */
            readonly harness: string;
            /** Model */
            readonly model?: string | null;
            /** Name */
            readonly name: string;
            /** Prompt */
            readonly prompt: string;
            /**
             * Touches
             * @default []
             */
            readonly touches: readonly string[];
        };
        /** ValidationError */
        readonly ValidationError: {
            /** Context */
            readonly ctx?: Record<string, never>;
            /** Input */
            readonly input?: unknown;
            /** Location */
            readonly loc: readonly (string | number)[];
            /** Message */
            readonly msg: string;
            /** Error Type */
            readonly type: string;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    readonly get_dashboard_api_dashboard_get: {
        readonly parameters: {
            readonly query?: {
                readonly period?: components["schemas"]["DashboardPeriod"];
            };
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["DashboardResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_system_snapshot_api_dashboard_system_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["SystemSnapshotResponse"] | null;
                };
            };
        };
    };
    readonly create_graph_api_graphs_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["CreateGraphRequest"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 201: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["CreatedGraphResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly plan_graph_api_graphs_plan_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["PlanGraphRequest"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 201: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["PlannedGraphResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_graph_api_graphs__session_id__get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["GraphResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly approve_graph_api_graphs__session_id__approve_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["GraphResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly run_graph_api_graphs__session_id__runs_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 202: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["GraphRunResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_sessions_api_sessions_get: {
        readonly parameters: {
            readonly query?: {
                readonly limit?: number;
            };
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["SessionResponse"][];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly create_session_api_sessions_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["CreateSessionRequest"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 201: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["CreatedSessionResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_session_api_sessions__session_id__get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["SessionResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly approve_api_sessions__session_id__approve_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["MergeResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_diff_api_sessions__session_id__diff_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["DiffResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly kill_run_api_sessions__session_id__kill_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_node_api_sessions__session_id__node_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["NodeResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_nodes_api_sessions__session_id__nodes_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["NodeResponse"][];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_node_api_sessions__session_id__nodes__node_id__get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["NodeResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly update_node_api_sessions__session_id__nodes__node_id__put: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["UpdateNodeRequest"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["NodeResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly delete_node_api_sessions__session_id__nodes__node_id__delete: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["GraphResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_acceptance_results_api_sessions__session_id__nodes__node_id__acceptance_get: {
        readonly parameters: {
            readonly query?: {
                readonly attempt?: number | null;
            };
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["AcceptanceResultResponse"][];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly resolve_acceptance_results_api_sessions__session_id__nodes__node_id__acceptance__attempt__patch: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
                readonly attempt: number;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["ReviewOutcomesRequest"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["AcceptanceResultResponse"][];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly approve_node_api_sessions__session_id__nodes__node_id__approve_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: {
            readonly content: {
                readonly "application/json": components["schemas"]["ReviewOutcomesRequest"] | null;
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["MergeResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly add_dependency_api_sessions__session_id__nodes__node_id__dependencies__depends_on_id__put: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
                readonly depends_on_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["GraphResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly remove_dependency_api_sessions__session_id__nodes__node_id__dependencies__depends_on_id__delete: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
                readonly depends_on_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["GraphResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_diff_api_sessions__session_id__nodes__node_id__diff_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["DiffResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly kill_node_api_sessions__session_id__nodes__node_id__kill_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly reject_node_api_sessions__session_id__nodes__node_id__reject_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody: {
            readonly content: {
                readonly "application/json": components["schemas"]["RejectRequest"];
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 202: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["NodeReviewResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly retry_node_api_sessions__session_id__nodes__node_id__retry_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: {
            readonly content: {
                readonly "application/json": components["schemas"]["RetryRequest"] | null;
            };
        };
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunOutcomeResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_reviews_api_sessions__session_id__nodes__node_id__reviews_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["NodeReviewResponse"][];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_runs_api_sessions__session_id__nodes__node_id__runs_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["RunResponse"][];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly run_node_api_sessions__session_id__nodes__node_id__runs_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunOutcomeResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_run_events_api_sessions__session_id__nodes__node_id__runs__run_id__events_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
                readonly run_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Canonical persisted AgentEvent array */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": unknown;
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_run_summary_api_sessions__session_id__nodes__node_id__runs__run_id__summary_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly node_id: string;
                readonly run_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunSummaryResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly retry_run_api_sessions__session_id__retry_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunOutcomeResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_runs_api_sessions__session_id__runs_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": readonly components["schemas"]["RunResponse"][];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly start_run_api_sessions__session_id__runs_post: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunOutcomeResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly list_run_events_api_sessions__session_id__runs__run_id__events_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly run_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Canonical persisted AgentEvent array */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": unknown;
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly get_run_summary_api_sessions__session_id__runs__run_id__summary_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path: {
                readonly session_id: string;
                readonly run_id: string;
            };
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["RunSummaryResponse"];
                };
            };
            /** @description Validation Error */
            readonly 422: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readonly health_health_get: {
        readonly parameters: {
            readonly query?: never;
            readonly header?: never;
            readonly path?: never;
            readonly cookie?: never;
        };
        readonly requestBody?: never;
        readonly responses: {
            /** @description Successful Response */
            readonly 200: {
                headers: {
                    readonly [name: string]: unknown;
                };
                content: {
                    readonly "application/json": components["schemas"]["HealthResponse"];
                };
            };
        };
    };
}
