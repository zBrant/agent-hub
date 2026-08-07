/**
 * GENERATED FILE — do not edit.
 * Run `pnpm gen:api`. See src/api/README.md and docs/architecture.md §7.
 */
export interface paths {
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
        /** CreateSessionRequest */
        readonly CreateSessionRequest: {
            /** Acceptance Criteria */
            readonly acceptance_criteria?: string | null;
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
        /** CreatedSessionResponse */
        readonly CreatedSessionResponse: {
            readonly node: components["schemas"]["NodeResponse"];
            readonly session: components["schemas"]["SessionResponse"];
        };
        /** DiffResponse */
        readonly DiffResponse: {
            /** Patch */
            readonly patch: string;
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
        /** NodeResponse */
        readonly NodeResponse: {
            /** Acceptance Criteria */
            readonly acceptance_criteria: string | null;
            /** Base Ref */
            readonly base_ref: string | null;
            /** Branch */
            readonly branch: string | null;
            /** Created Ms */
            readonly created_ms: number;
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
            /** Updated Ms */
            readonly updated_ms: number;
            /** Worktree Path */
            readonly worktree_path: string | null;
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
