// First-party plugin chat component: the artifact plugin's `artifact-ref` chip (#3617). The
// plugin registers the KIND server-side (`registry.register_component`, which validates the
// props); this registers its console renderer + live hook through the same ext seam a fork
// would use — no edit to ChatComponent.tsx. Without the plugin the kind never reaches the
// console (the server won't extract it), and a chip replayed from an older history renders
// inert ("the Artifact panel is off").
import { ArtifactRefChip } from "../artifacts/ArtifactRefChip";
import { ARTIFACT_REF_COMPONENT, onLiveArtifactRef } from "../artifacts/artifactRef";
import { registerChatComponent } from "./componentRegistry";

registerChatComponent(ARTIFACT_REF_COMPONENT, ArtifactRefChip, { onLive: onLiveArtifactRef });
