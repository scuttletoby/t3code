// Pins what a chat link to a file on another environment relies on: upstream's `assets.createUrl`
// serves an absolute file without looking up the thread, so the fork's fixed placeholder thread id
// works on any environment. The real RPC layer runs with every other service stubbed, as ws.test.ts.
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import * as SecretResolver from "@t3tools/secret-provider-core/server/SecretResolver";
import * as ProviderLatestVersions from "@t3tools/provider-core/server/ProviderLatestVersions";
import * as RelayClient from "@t3tools/shared/relayClient";
import { assert, it } from "@effect/vitest";
import {
  AuthFilesystemReadScope,
  AuthSessionId,
  ENVIRONMENT_FILE_LINK_THREAD_ID,
  WS_METHODS,
  WsRpcGroup,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as RpcTest from "effect/rpc/RpcTest";

import { ASSET_ROUTE_PREFIX, resolveAsset } from "../assets/AssetAccess.ts";
import { layerWsRpc } from "../ws.ts";
import * as AcpRegistryRuntimeCoordinator from "@t3tools/provider-acp-registry/server/AcpRegistryRuntimeCoordinator";
import * as AcpRegistrySessions from "../provider/AcpRegistrySessions.ts";
import * as AcpRegistrySupport from "@t3tools/provider-acp-registry/server/AcpRegistrySupport";
import * as AgentSessionImporter from "../project/AgentSessionImporter.ts";
import * as AgentSessionScanner from "../project/AgentSessionScanner.ts";
import * as AnalyticsService from "../telemetry/AnalyticsService.ts";
import * as AntigravityInstallation from "../provider/AntigravityInstallation.ts";
import * as AssetSigner from "../assets/AssetSigner.ts";
import * as NativeAppIconResolver from "../assets/NativeAppIconResolver.ts";
import * as BackgroundPolicy from "../background/BackgroundPolicy.ts";
import * as CheckpointDiffQuery from "../checkpointing/CheckpointDiffQuery.ts";
import * as CodexInstallation from "../provider/CodexInstallation.ts";
import * as CommandOutputHub from "../orchestration-v2/CommandOutputHub.ts";
import * as DatabaseStorage from "../persistence/DatabaseStorage.ts";
import * as DeviceService from "../device/DeviceService.ts";
import * as DirectEndpoints from "../environment/DirectEndpoints.ts";
import * as DocumentText from "../provider/DocumentText.ts";
import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as EnvironmentTheme from "../environmentTheme.ts";
import * as ExternalLauncher from "../process/externalLauncher.ts";
import * as GitWorkflowService from "../git/GitWorkflowService.ts";
import * as GuestShareService from "../guest/GuestShareService.ts";
import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as IssueService from "../issue/IssueService.ts";
import * as IssueTrackerConnections from "../issue/IssueTrackerConnections.ts";
import * as Keybindings from "../keybindings.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ManagedProjectFolders from "../project/ManagedProjectFolders.ts";
import * as McpAppRequests from "../mcpApps/McpAppRequests.ts";
import * as McpSignIn from "../mcpApps/McpSignIn.ts";
import * as ModelManifest from "../provider/ModelManifest.ts";
import * as NativeChildWorkQuery from "../orchestration-v2/NativeChildWorkQuery.ts";
import * as NpmRelease from "../cloud/npmRelease.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as OtlpEndpointCheck from "../observability/OtlpEndpointCheck.ts";
import * as PairingGrantStore from "../auth/PairingGrantStore.ts";
import * as PeerClientPairing from "../peer/PeerClientPairing.ts";
import * as PeerLinkRequests from "../peer/PeerLinkRequests.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as PeerThreadRelay from "../peer/PeerThreadRelay.ts";
import * as PortScanner from "../preview/PortScanner.ts";
import * as PreviewAutomationBroker from "../mcp/PreviewAutomationBroker.ts";
import * as PreviewManager from "../preview/Manager.ts";
import * as ProcessDiagnostics from "../diagnostics/ProcessDiagnostics.ts";
import * as ProcessResourceMonitor from "../diagnostics/ProcessResourceMonitor.ts";
import * as ProjectCloneTracker from "../project/ProjectCloneTracker.ts";
import * as ProjectEnrichment from "../project/ProjectEnrichmentService.ts";
import * as ProjectFaviconResolver from "../project/ProjectFaviconResolver.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as ProjectSetupScriptRunner from "../project/ProjectSetupScriptRunner.ts";
import * as ProjectStore from "../orchestration-v2/ProjectStore.ts";
import * as ProviderAuthService from "../provider/ProviderAuthService.ts";
import * as ProviderInstallation from "../provider/providerInstallation.ts";
import * as ProviderInstanceRegistry from "../provider/ProviderInstanceRegistry.ts";
import * as ProviderMaintenanceRunner from "../provider/providerMaintenanceRunner.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ProviderSessionManager from "../orchestration-v2/ProviderSessionManager.ts";
import * as PullRequestReviewGuideService from "../pullRequest/PullRequestReviewGuideService.ts";
import * as PullRequestService from "../pullRequest/PullRequestService.ts";
import * as PullRequestSyncReactor from "../orchestration-v2/PullRequestSyncReactor.ts";
import * as RemoteOpenTargets from "../environment/RemoteOpenTargets.ts";
import * as RepositoryIdentityResolver from "../project/RepositoryIdentityResolver.ts";
import * as ResourceTelemetry from "../resourceTelemetry/ResourceTelemetry.ts";
import * as ReviewService from "../review/ReviewService.ts";
import * as RpcAuthorization from "../auth/RpcAuthorization.ts";
import * as ScheduledTasks from "../scheduledTasks/ScheduledTaskService.ts";
import * as ScratchThreadPromotion from "../project/ScratchThreadPromotion.ts";
import * as SecretCheck from "../secrets/SecretCheck.ts";
import * as SecretRequests from "../secrets/SecretRequests.ts";
import * as ServerBrowser from "../preview/ServerBrowser.ts";
import * as ServerConfigModule from "../config.ts";
import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import * as ServerLifecycleEvents from "../serverLifecycleEvents.ts";
import * as ServerRuntimeStartup from "../serverRuntimeStartup.ts";
import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerSelfUpdate from "../cloud/selfUpdate.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SessionStore from "../auth/SessionStore.ts";
import * as SourceControlDiscovery from "../sourceControl/SourceControlDiscovery.ts";
import * as SourceControlRepositoryService from "../sourceControl/SourceControlRepositoryService.ts";
import * as SourceControlSignIn from "../sourceControl/SourceControlSignIn.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as StorageCleanup from "../storageCleanup.ts";
import * as TaskFolders from "../workspace/TaskFolders.ts";
import * as TerminalManager from "../terminal/Manager.ts";
import * as TextGenerators from "../textGeneration/TextGenerators.ts";
import * as ThreadIssueService from "../orchestration-v2/ThreadIssueService.ts";
import * as ThreadLaunchService from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ThreadSearch from "../orchestration-v2/ThreadSearch.ts";
import * as ThreadViewers from "../orchestration-v2/ThreadViewers.ts";
import * as TraceDiagnostics from "../diagnostics/TraceDiagnostics.ts";
import * as TriageService from "../triage/TriageService.ts";
import * as UsageLimitSources from "../usage/UsageLimitSources.ts";
import * as UsageService from "../usage/UsageService.ts";
import * as VcsProvisioningService from "../vcs/VcsProvisioningService.ts";
import * as VcsStatusBroadcaster from "../vcs/VcsStatusBroadcaster.ts";
import * as WorkspaceEntries from "../workspace/WorkspaceEntries.ts";
import * as WorkspaceFileSystem from "../workspace/WorkspaceFileSystem.ts";
import * as WorkspaceFolders from "../workspace/WorkspaceFolders.ts";
import * as WorkspacePaths from "../workspace/WorkspacePaths.ts";
import * as WorkspacePreviewRevisions from "../assets/WorkspacePreviewRevisions.ts";
import * as WorktreeSetupTracker from "../project/WorktreeSetupTracker.ts";

const assetsCreateUrlOnly = WsRpcGroup.omit(
  ...[...WsRpcGroup.requests.keys()].filter(
    (tag): tag is Exclude<keyof typeof RpcAuthorization.RPC_REQUIRED_SCOPES, "assets.createUrl"> =>
      tag !== WS_METHODS.assetsCreateUrl,
  ),
);

const layerStubbedServices = Layer.mergeAll(
  Layer.mock(DocumentText.DocumentText)({}),
  Layer.mock(ServerSecretStore.ServerSecretStore)({}),
  Layer.mock(SecretCheck.SecretCheck)({}),
  Layer.mock(WorkspacePaths.WorkspacePaths)({}),
  Layer.mock(TraceDiagnostics.TraceDiagnostics)({}),
  Layer.mock(TriageService.TriageService)({}),
  Layer.mock(TextGenerators.TextGenerators)({}),
  Layer.mock(ProjectFaviconResolver.ProjectFaviconResolver)({}),
  Layer.mock(ProjectEnrichment.ProjectEnrichmentService)({}),
  Layer.mock(AcpRegistryRuntimeCoordinator.AcpRegistryRuntimeCoordinator)({}),
  Layer.mock(AcpRegistrySupport.AcpRegistryCatalog)({}),
  Layer.mock(AcpRegistrySessions.AcpRegistrySessions)({}),
  Layer.mock(AgentSessionImporter.AgentSessionImporter)({}),
  Layer.mock(AgentSessionScanner.AgentSessionScanner)({}),
  Layer.mock(AnalyticsService.AnalyticsService)({}),
  Layer.mock(AntigravityInstallation.AntigravityInstallation)({ managedDirectory: "/unused" }),
  Layer.mock(BackgroundPolicy.BackgroundPolicy)({}),
  Layer.mock(CheckpointDiffQuery.CheckpointDiffQuery)({}),
  Layer.mock(NativeChildWorkQuery.NativeChildWorkQuery)({}),
  Layer.mock(CommandOutputHub.CommandOutputHub)({}),
  ThreadViewers.layer,
  Layer.mock(CodexInstallation.CodexInstallation)({ managedDirectory: "/unused" }),
  Layer.mock(DeviceService.DeviceService)({}),
  Layer.mock(EnvironmentTheme.EnvironmentThemeService)({}),
  Layer.mock(GitWorkflowService.GitWorkflowService)({}),
  Layer.mock(HostResources.HostResources)({}),
  Layer.mock(McpAppRequests.McpAppRequests)({}),
  Layer.mock(McpSignIn.McpSignIn)({}),
  Layer.mock(ModelManifest.ModelManifest)({}),
  Layer.mock(OrchestrationEventStore.OrchestrationEventStore)({}),
  Layer.mock(Orchestrator.OrchestratorV2)({}),
  Layer.mock(OtlpEndpointCheck.OtlpEndpointCheck)({}),
  Layer.mock(PairingGrantStore.PairingGrantStore)({}),
  Layer.mock(PeerLinks.PeerLinks)({}),
  Layer.mock(PeerLinkRequests.PeerLinkRequests)({}),
  Layer.mock(PeerThreadRelay.PeerThreadRelay)({}),
  Layer.mock(PeerClientPairing.PeerClientPairing)({}),
  Layer.mock(PortScanner.PortDiscovery)({ trackAgent: () => () => undefined }),
  Layer.mock(PreviewManager.PreviewManager)({}),
  Layer.mock(ProcessDiagnostics.ProcessDiagnostics)({}),
  Layer.mock(ProcessResourceMonitor.ProcessResourceMonitor)({}),
  Layer.mock(ProjectCloneTracker.ProjectCloneTracker)({}),
  Layer.mock(ProjectService.ProjectService)({}),
  Layer.mock(ProjectSetupScriptRunner.ProjectSetupScriptRunner)({}),
  Layer.mock(ProjectStore.ProjectStoreV2)({}),
  Layer.mock(ProviderAuthService.ProviderAuthService)({}),
  Layer.mock(ProviderInstallation.ProviderInstallation)({}),
  Layer.mock(ProviderInstanceRegistry.ProviderInstanceRegistry)({}),
  Layer.mock(ProviderLatestVersions.ProviderLatestVersions)({}),
  Layer.mock(ProviderMaintenanceRunner.ProviderMaintenanceRunner)({}),
  Layer.mock(ProviderSessionManager.ProviderSessionManagerV2)({}),
  Layer.mock(PullRequestReviewGuideService.PullRequestReviewGuideService)({}),
  Layer.mock(PullRequestService.PullRequestService)({}),
  Layer.mock(IssueService.IssueService)({}),
  Layer.mock(ThreadIssueService.ThreadIssueService)({}),
  Layer.mock(IssueTrackerConnections.IssueTrackerConnections)({}),
  Layer.mock(PullRequestSyncReactor.PullRequestSyncReactor)({}),
  Layer.mock(RelayClient.RelayClient)({}),
  Layer.mock(RepositoryIdentityResolver.RepositoryIdentityResolver)({}),
  Layer.mock(ResourceTelemetry.ResourceTelemetry)({}),
  Layer.mock(ReviewService.ReviewService)({}),
  Layer.mock(ScheduledTasks.ScheduledTaskService)({}),
  Layer.mock(SecretRequests.SecretRequests)({}),
  Layer.mock(SecretResolver.SecretResolver)({}),
  Layer.mock(GuestShareService.GuestShareService)({}),
  Layer.mock(ServerLifecycleEvents.ServerLifecycleEvents)({}),
  Layer.mock(ServerRuntimeStartup.ServerRuntimeStartup)({}),
  Layer.mock(ServerSelfUpdate.ServerSelfUpdate)({}),
  Layer.mock(NpmRelease.NpmRelease)({}),
  Layer.mock(SessionStore.SessionStore)({ cookieName: "config-test", legacyCookieName: undefined }),
  Layer.mock(SourceControlDiscovery.SourceControlDiscovery)({}),
  Layer.mock(SourceControlRepositoryService.SourceControlRepositoryService)({}),
  Layer.mock(SourceControlSignIn.SourceControlSignIn)({}),
  Layer.mock(StorageCleanup.StorageCleanup)({}),
  Layer.mock(DatabaseStorage.DatabaseStorage)({}),
  Layer.mock(TerminalManager.TerminalManager)({}),
  Layer.mock(ThreadLaunchService.ThreadLaunchService)({}),
  Layer.mock(ThreadManagement.ThreadManagementService)({}),
  Layer.mock(ThreadSearch.ThreadSearch)({}),
  Layer.mock(UsageService.UsageService)({}),
  Layer.mock(VcsProvisioningService.VcsProvisioningService)({}),
  Layer.mock(VcsStatusBroadcaster.VcsStatusBroadcaster)({}),
  Layer.mock(WorkspaceEntries.WorkspaceEntries)({}),
  Layer.mock(WorkspaceFolders.WorkspaceFolders)({}),
  Layer.mock(TaskFolders.TaskFolders)({}),
  Layer.mock(WorkspaceFileSystem.WorkspaceFileSystem)({}),
  Layer.mock(WorktreeSetupTracker.WorktreeSetupTracker)({}),
);

const layerRealAssetServices = Layer.mergeAll(
  Keybindings.layer,
  AssetSigner.layer.pipe(Layer.provide(ServerSecretStore.layer)),
  WorkspacePreviewRevisions.layer,
  ServerSettings.layer.pipe(
    Layer.provide(Layer.merge(ServerSecretStore.layer, ManagedPolicy.layerTest())),
  ),
).pipe(
  Layer.provideMerge(SqlitePersistence.layerMemory),
  Layer.provideMerge(
    ServerConfigModule.layerTest(process.cwd(), { prefix: "t3-environment-file-link-test-" }),
  ),
  Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)),
);

it.effect(
  "assets.createUrl serves an absolute file for a thread the environment does not have",
  () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const directory = yield* fs.makeTempDirectoryScoped({ prefix: "t3-environment-file-link-" });
      const filePath = path.join(directory, "app.log");
      yield* fs.writeFileString(filePath, "log lines");
      const services = Layer.mergeAll(
        layerStubbedServices,
        Layer.mock(ExternalLauncher.ExternalLauncher)({}),
        Layer.mock(ProviderRegistry.ProviderRegistry)({}),
        Layer.mock(UsageLimitSources.UsageLimitSources)({}),
        Layer.mock(RemoteOpenTargets.RemoteOpenTargets)({}),
        Layer.mock(DirectEndpoints.DirectEndpoints)({}),
        Layer.mock(ManagedProjectFolders.ManagedProjectFolders)({ namedProjectsRoot: "/unused" }),
        Layer.mock(ScratchThreadPromotion.ScratchThreadPromotion)({}),
        Layer.mock(ServerEnvironment.ServerEnvironment)({}),
        Layer.mock(EnvironmentAuth.EnvironmentAuth)({}),
        Layer.mock(PreviewAutomationBroker.PreviewAutomationBroker)({}),
      );
      const client = yield* RpcTest.makeClient(assetsCreateUrlOnly).pipe(
        Effect.provide(
          layerWsRpc(
            {
              sessionId: AuthSessionId.make("environment-file-link-test"),
              subject: "environment-file-link-test",
              method: "browser-session-cookie",
              scopes: [],
            },
            {},
            yield* ServerBrowser.ServerBrowser,
            () => Effect.void,
            { secretReferences: false },
          ).pipe(
            Layer.provide(services),
            Layer.merge(RpcAuthorization.layer([AuthFilesystemReadScope])),
          ),
        ),
      );
      const createUrl = (requested: string) =>
        client[WS_METHODS.assetsCreateUrl]({
          resource: {
            _tag: "media-file",
            threadId: ENVIRONMENT_FILE_LINK_THREAD_ID,
            path: requested,
            disposition: "attachment",
          },
        });

      // Asset signing and the file system come from the outer layer, so this test can read the URL.
      // The stubbed thread service has no threads: any lookup fails, as a relative path shows.
      assert.isTrue(Exit.isFailure(yield* createUrl("app.log").pipe(Effect.exit)));
      const { relativeUrl } = yield* createUrl(filePath);
      assert.isTrue(relativeUrl.startsWith(`${ASSET_ROUTE_PREFIX}/`));
      const suffix = relativeUrl.slice(`${ASSET_ROUTE_PREFIX}/`.length);
      const separator = suffix.indexOf("/");
      const asset = yield* resolveAsset(suffix.slice(0, separator), suffix.slice(separator + 1));
      assert.deepInclude(asset, { kind: "file", download: true, fileName: "app.log" });
    }).pipe(
      Effect.provide(
        Layer.mergeAll(
          Layer.mock(ServerBrowser.ServerBrowser)({}),
          // A file asset never uses these; resolving other kinds of asset needs them.
          Layer.mock(NativeAppIconResolver.NativeAppIconResolver)({}),
          Layer.mock(Orchestrator.OrchestratorV2)({}),
          Layer.mock(WorkspacePaths.WorkspacePaths)({}),
          layerRealAssetServices,
        ),
      ),
    ),
);
