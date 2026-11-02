"use client";

import { Fragment } from "react";

import { Banner, Panel } from "@/components/ui";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import type { Specialist } from "@/lib/types";

interface AgentsView {
  specialists: Specialist[];
  topology: string;
  engine: string;
  lanes: Record<string, boolean>;
  note: string;
}

export default function AgentsPage() {
  const { data, error } = useAsync<AgentsView>(async () => {
    const [agents, capabilities] = await Promise.all([api.agents(), api.capabilities()]);
    return {
      specialists: agents.specialists,
      topology: agents.orchestration.topology,
      engine: agents.orchestration.engine,
      lanes: capabilities.storage_lanes,
      note: capabilities.note,
    };
  }, []);

  if (error) {
    return (
      <Panel title="Agents">
        <Banner kind="error">{error}</Banner>
      </Panel>
    );
  }

  return (
    <>
      <Panel
        title="Who is asking you to approve this"
        subtitle="Four passes, each with its own question, its own prompt version and an explicit capability boundary. None of them may post to GitHub."
      >
        <table>
          <thead>
            <tr>
              <th>Pass</th>
              <th>Question</th>
              <th>Model</th>
              <th>Prompt</th>
              <th>Capabilities</th>
              <th>May post</th>
            </tr>
          </thead>
          <tbody>
            {data?.specialists.map((specialist) => (
              <tr key={specialist.agent}>
                <td>
                  <strong>{specialist.agent}</strong>
                </td>
                <td className="muted">{specialist.concern ?? "-"}</td>
                <td className="mono">{specialist.model}</td>
                <td className="mono">
                  {specialist.prompt.version} · {specialist.prompt.hash}
                </td>
                <td className="mono muted">{specialist.capabilities}</td>
                <td>{specialist.may_post_to_github ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Orchestration" subtitle={`Engine: ${data?.engine ?? "-"}`}>
        <pre className="mono" style={{ overflowX: "auto", margin: 0 }}>
          {data?.topology ?? "loading…"}
        </pre>
      </Panel>

      <Panel
        title="Storage lanes actually present"
        subtitle="The one-spine design degrades to slower-but-correct rather than failing when an optional extension is missing."
      >
        <div className="kv">
          {Object.entries(data?.lanes ?? {}).map(([lane, present]) => (
            <Fragment key={lane}>
              <dt>{lane.replace(/_/g, " ")}</dt>
              <dd className={present ? "" : "muted"} style={{ margin: 0 }}>
                {present ? "present" : "absent"}
              </dd>
            </Fragment>
          ))}
        </div>
        <p className="muted">{data?.note}</p>
      </Panel>
    </>
  );
}
