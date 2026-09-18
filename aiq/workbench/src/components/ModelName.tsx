// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Human-readable model name with the id underneath — never a bare id.
import { useModels } from '../lib/models';

const LANE_LABEL: Record<string, string> = {
  converse: 'Converse',
  bedrock_runtime: 'Converse',
  mantle_chat: 'Mantle · chat',
  mantle_responses: 'Mantle · responses',
  mantle_messages: 'Mantle · messages',
  bedrock_mantle: 'Mantle',
};
export const laneLabel = (lane: string | null | undefined): string => (lane ? LANE_LABEL[lane] ?? lane : '—');

export function ModelName({ id, name, lane, inline, showId = true }: { id: string | null | undefined; name?: string | null; lane?: string | null; inline?: boolean; showId?: boolean }) {
  const models = useModels(false);
  const label = name ?? models.nameFor(id, null);
  if (!id) return <span className="muted">—</span>;
  if (inline) {
    return (
      <span title={`${id}${lane ? ` · ${laneLabel(lane)}` : ''}`}>
        <span className="model-name">{label ?? id}</span>
        {showId && label && label !== id ? <span className="model-id"> {id}</span> : null}
      </span>
    );
  }
  return (
    <span style={{ display: 'inline-flex', flexDirection: 'column', minWidth: 0 }}>
      <span className="model-name">{label ?? id}</span>
      {showId ? (
        <span className="model-id ellipsis" title={id}>
          {id}
          {lane ? ` · ${laneLabel(lane)}` : ''}
        </span>
      ) : null}
    </span>
  );
}
