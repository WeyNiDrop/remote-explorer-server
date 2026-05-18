using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace RemoteExplorer
{
    public enum RemoteStreamMode
    {
        UdpJpeg,
        WebRtc
    }

    [DefaultExecutionOrder(-10000)]
    public class RemoteExplorerApp : MonoBehaviour
    {
        private static RemoteExplorerApp instance;
        private static int nextInstanceId;

        private const float RemotePreviewPreferredHeight = 780f;
        private const float RemotePreviewMinHeight = 320f;
        private const float StreamStatusIntervalSeconds = 0.5f;
        private const float StreamWatchdogSeconds = 12f;
        private const float StreamRestartCooldownSeconds = 12f;
        private const float StreamRenderLogIntervalSeconds = 5f;

        private readonly RemoteExplorerClient client = new RemoteExplorerClient();
        private readonly List<DiscoveredServer> servers = new List<DiscoveredServer>();
        private RemoteExplorerSettings settings;
        private CancellationTokenSource lifetime;

        private Dropdown serverDropdown;
        private InputField passwordInput;
        private Toggle autoConnectToggle;
        private InputField urlInput;
        private Text statusText;
        private Text remoteStatusText;
        private GameObject connectPage;
        private GameObject remotePage;
        private Button connectButton;
        private Button openButton;
        private Button closeButton;
        private Button backButton;
        private Button forwardButton;
        private Button reloadButton;
        private GameObject remoteInputPanel;
        private InputField remoteTextInput;
        private Button remoteInputDoneButton;
        private Button remoteInputCancelButton;
        private Button mediaStatusButton;
        private Button mediaPreviousButton;
        private Button mediaSeekBackButton;
        private Button mediaPlayPauseButton;
        private Button mediaSeekForwardButton;
        private Button mediaNextButton;
        private Button mediaFullscreenButton;
        private Button mediaVolumeDownButton;
        private Button mediaMuteButton;
        private Button mediaVolumeUpButton;
        private readonly Queue<RemoteKeyboardCommand> remoteKeyboardCommands = new Queue<RemoteKeyboardCommand>();
        private Dropdown streamModeDropdown;
        private Dropdown streamResolutionDropdown;
        private Slider streamFpsSlider;
        private Text streamFpsText;
        private Button streamToggleButton;
        private RawImage streamImage;
        private Text streamStatusText;
        private Texture2D streamTexture;
#if REMOTE_EXPLORER_HAS_WEBRTC
        private RemoteExplorerWebRtcPlayback webRtcPlayback;
#endif
        private int latestSourceWidth = 1;
        private int latestSourceHeight = 1;
        private float lastStreamFrameAt = -1f;
        private float nextStreamStatusAt = -1f;
        private float nextStreamWatchdogAt = -1f;
        private float streamRenderStatsSince = -1f;
        private float nextStreamRenderLogAt = -1f;
        private int streamRenderFrames;
        private int streamRenderFrameGaps;
        private float streamRenderDecodeMsTotal;
        private float streamRenderDecodeMsMax;
        private float streamRenderApplyMsTotal;
        private float streamRenderApplyMsMax;
        private uint streamRenderLastFrameId;
        private bool streamRenderHasFrameId;
        private bool streamRestartInFlight;
        private bool streamStartInFlight;
        private bool streamSettingsDirty;
        private float streamSettingsApplyAt = -1f;
        private RemoteStreamMode activeStreamMode = RemoteStreamMode.UdpJpeg;
        private bool hasActiveStreamMode;
        private bool suppressRemoteInputEndEdit;
        private bool remoteInputCommitInFlight;
        private bool remoteKeyboardFlushInFlight;
        private int remoteInputVersion;
        private bool canvasReady;
        private string fallbackPassword = string.Empty;
        private string fallbackUrl = "https://example.com";
        private string currentStatus = "Starting...";
        private string fatalUiError;
        private int instanceId;

        private struct RemoteKeyboardCommand
        {
            public string Kind;
            public string Value;
        }

        private void Awake()
        {
            RemoteExplorerDiagnostics.Initialize();
            if (instance != null && instance != this)
            {
                RemoteExplorerDiagnostics.Info(
                    $"Duplicate RemoteExplorerApp destroyed name={gameObject.name} existing={instance.gameObject.name}");
                Destroy(gameObject);
                return;
            }

            instance = this;
            instanceId = ++nextInstanceId;
            DontDestroyOnLoad(gameObject);
            RemoteExplorerDiagnostics.Info($"RemoteExplorerApp awake instance={instanceId}");

            lifetime = new CancellationTokenSource();
            settings = RemoteExplorerSettings.Load();
            fallbackPassword = settings.Password ?? string.Empty;
            try
            {
                BuildUi();
                ApplySettingsToUi();
                canvasReady = true;
                RemoteExplorerDiagnostics.Info("Canvas UI created.");
            }
            catch (Exception ex)
            {
                canvasReady = false;
                fatalUiError = ex.ToString();
                Debug.LogException(ex);
            }
        }

        private async void Start()
        {
            SetStatus("Ready");
            await RefreshServersAsync();
            if (settings.AutoConnect)
            {
                await TryAutoConnectAsync();
            }
        }

        private void OnDestroy()
        {
            if (instance == this)
            {
                instance = null;
            }

            lifetime?.Cancel();
            lifetime?.Dispose();
            client.Dispose();
        }

        private void Update()
        {
            RemoteStreamFrame latestFrame = null;
            RemoteStreamFrame frame;
            while (client.TryDequeueStreamFrame(out frame))
            {
                latestFrame = frame;
            }

            if (latestFrame != null)
            {
                ApplyStreamFrame(latestFrame);
            }

            if (streamSettingsDirty && Time.unscaledTime >= streamSettingsApplyAt)
            {
                streamSettingsDirty = false;
                FireAndForget(ApplyStreamSettingsAsync);
            }

            UpdateStreamWatchdog();
            PollRemoteKeyboardInput();
        }

        private void OnGUI()
        {
            if (canvasReady)
            {
                return;
            }

            const int margin = 24;
            const int row = 42;
            var width = Mathf.Min(Screen.width - margin * 2, 760);
            var y = margin;

            GUI.Box(new Rect(margin, y, width, 360), "Remote Explorer");
            y += row;
            GUI.Label(new Rect(margin + 16, y, width - 32, row), currentStatus);
            y += row;

            if (!string.IsNullOrEmpty(fatalUiError))
            {
                GUI.Label(new Rect(margin + 16, y, width - 32, row * 2), "Canvas UI failed. Using fallback controls. Check Console for details.");
                y += row * 2;
            }

            if (GUI.Button(new Rect(margin + 16, y, 150, row - 6), "Discover"))
            {
                FireAndForget(RefreshServersAsync);
            }

            var serverLabel = servers.Count == 0 ? "No servers found" : servers[0].ToString();
            GUI.Label(new Rect(margin + 180, y, width - 196, row), serverLabel);
            y += row;

            GUI.Label(new Rect(margin + 16, y, 120, row), "Password");
            fallbackPassword = GUI.PasswordField(new Rect(margin + 140, y + 4, width - 156, row - 10), fallbackPassword, '*');
            y += row;

            if (GUI.Button(new Rect(margin + 16, y, 150, row - 6), "Connect"))
            {
                if (servers.Count > 0)
                {
                    FireAndForget(() => ConnectFallbackAsync(servers[0]));
                }
                else
                {
                    SetStatus("Discover a server first");
                }
            }
            y += row;

            GUI.Label(new Rect(margin + 16, y, 120, row), "URL");
            fallbackUrl = GUI.TextField(new Rect(margin + 140, y + 4, width - 156, row - 10), fallbackUrl);
            y += row;

            if (GUI.Button(new Rect(margin + 16, y, 150, row - 6), "Open"))
            {
                FireAndForget(() => RunCommandAsync(() => client.NavigateAsync(fallbackUrl, lifetime.Token), "Opened"));
            }

            if (GUI.Button(new Rect(margin + 180, y, 150, row - 6), "Close Page"))
            {
                FireAndForget(() => RunCommandAsync(() => client.ClosePageAsync(lifetime.Token), "Closed page"));
            }
        }

        private void BuildUi()
        {
            EnsureEventSystem();

            var canvasObject = CreateUiObject("Remote Explorer Canvas", transform);
            canvasObject.transform.SetParent(transform, false);
            var canvas = canvasObject.AddComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 1000;
            canvasObject.AddComponent<GraphicRaycaster>();
            var scaler = canvasObject.AddComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = Screen.width >= Screen.height
                ? new Vector2(1280, 720)
                : new Vector2(1080, 1920);
            scaler.matchWidthOrHeight = 0.5f;

            var root = CreatePanel(canvasObject.transform, "Root", new Color(0.06f, 0.07f, 0.09f, 1f));
            Stretch(root.GetComponent<RectTransform>(), 0, 0, 0, 0);
            var layout = root.AddComponent<VerticalLayoutGroup>();
            layout.padding = new RectOffset(0, 0, 0, 0);
            layout.spacing = 0;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = true;
            layout.childForceExpandHeight = true;

            connectPage = CreatePage(root.transform, "Connect Page", 36, 36, 18);
            BuildConnectPage(connectPage.transform);

            remotePage = CreatePage(root.transform, "Remote Page", 12, 10, 6);
            BuildRemotePage(remotePage.transform);
            remotePage.SetActive(false);

            SetCommandButtons(false);
        }

        private void BuildConnectPage(Transform parent)
        {
            CreateText(parent, "Remote Explorer", 42, FontStyle.Bold, TextAnchor.MiddleLeft, 64);
            statusText = CreateText(parent, "Starting...", 24, FontStyle.Normal, TextAnchor.MiddleLeft, 48);

            var discoveryRow = CreateRow(parent, "Discovery Row", 70);
            serverDropdown = CreateDropdown(discoveryRow.transform);
            CreateButton(discoveryRow.transform, "Discover", RefreshButtonClicked, 220);

            passwordInput = CreateInput(parent, "Password", 72, true);
            autoConnectToggle = CreateToggle(parent, "Auto connect and save password", 58);

            var connectRow = CreateRow(parent, "Connect Row", 70);
            connectButton = CreateButton(connectRow.transform, "Connect", ConnectButtonClicked, 260);
            CreateButton(connectRow.transform, "Save", SaveSettings, 180);
        }

        private void BuildRemotePage(Transform parent)
        {
            var headerRow = CreateCompactRow(parent, "Remote Header Row", 44);
            var headerTitle = CreateText(headerRow.transform, "Remote Browser", 24, FontStyle.Bold, TextAnchor.MiddleLeft, 44);
            headerTitle.GetComponent<LayoutElement>().flexibleWidth = 1;
            CreateCompactButton(headerRow.transform, "Servers", ShowConnectPage, 150);

            remoteStatusText = CreateText(parent, "Not connected", 16, FontStyle.Normal, TextAnchor.MiddleLeft, 22);
            streamStatusText = CreateText(parent, "Stream stopped", 15, FontStyle.Normal, TextAnchor.MiddleLeft, 20);

            var urlRow = CreateCompactRow(parent, "Remote URL Row", 50);
            urlInput = CreateInput(urlRow.transform, "https://example.com", 50, false);
            openButton = CreateCompactButton(urlRow.transform, "Open", OpenButtonClicked, 110);
            closeButton = CreateCompactButton(urlRow.transform, "Close", CloseButtonClicked, 110);

            var streamOptionsRow = CreateCompactRow(parent, "Stream Options Row", 50);
            streamModeDropdown = CreateDropdown(streamOptionsRow.transform);
            var modeLayout = streamModeDropdown.GetComponent<LayoutElement>() ??
                streamModeDropdown.gameObject.AddComponent<LayoutElement>();
            modeLayout.minWidth = 178;
            modeLayout.preferredWidth = 178;
            modeLayout.flexibleWidth = 0;
            streamModeDropdown.ClearOptions();
#if REMOTE_EXPLORER_HAS_WEBRTC
            streamModeDropdown.AddOptions(new List<string> { "WebRTC/H.264", "UDP/JPEG" });
            streamModeDropdown.value = PreferredStreamModeIndex(settings.StreamMode);
#else
            streamModeDropdown.AddOptions(new List<string> { "UDP/JPEG" });
            streamModeDropdown.value = 0;
            streamModeDropdown.interactable = false;
#endif
            streamModeDropdown.onValueChanged.AddListener(_ => ScheduleStreamSettingsApply());

            streamResolutionDropdown = CreateDropdown(streamOptionsRow.transform);
            var dropdownLayout = streamResolutionDropdown.GetComponent<LayoutElement>() ??
                streamResolutionDropdown.gameObject.AddComponent<LayoutElement>();
            dropdownLayout.minWidth = 126;
            dropdownLayout.preferredWidth = 126;
            dropdownLayout.flexibleWidth = 0;
            streamResolutionDropdown.ClearOptions();
            streamResolutionDropdown.AddOptions(new List<string> { "360p", "540p", "720p", "1080p" });
            streamResolutionDropdown.value = 0;
            streamResolutionDropdown.onValueChanged.AddListener(_ => ScheduleStreamSettingsApply());

            streamFpsText = CreateText(streamOptionsRow.transform, "FPS 30", 18, FontStyle.Bold, TextAnchor.MiddleCenter, 50);
            var fpsLayout = streamFpsText.GetComponent<LayoutElement>();
            fpsLayout.minWidth = 96;
            fpsLayout.preferredWidth = 96;
            fpsLayout.flexibleWidth = 0;
            streamFpsSlider = CreateSlider(
                streamOptionsRow.transform,
                RemoteExplorerClient.MinStreamFps,
                RemoteExplorerClient.MaxStreamFps,
                RemoteExplorerClient.DefaultStreamFps,
                220);
            streamFpsSlider.onValueChanged.AddListener(_ =>
            {
                UpdateStreamFpsLabel();
                ScheduleStreamSettingsApply();
            });
            streamToggleButton = CreateCompactButton(streamOptionsRow.transform, "Start", () => FireAndForget(ToggleStreamAsync), 110);

            var navRow = CreateCompactRow(parent, "Remote Navigation Row", 50);
            backButton = CreateCompactButton(navRow.transform, "Back", () => FireAndForget(() => SendSimpleCommandAsync("back")), 118);
            forwardButton = CreateCompactButton(navRow.transform, "Forward", () => FireAndForget(() => SendSimpleCommandAsync("forward")), 136);
            reloadButton = CreateCompactButton(navRow.transform, "Reload", () => FireAndForget(() => SendSimpleCommandAsync("reload")), 126);
            CreateCompactButton(navRow.transform, "Status", () => FireAndForget(() => SendSimpleCommandAsync("status")), 118);

            var mediaRow = CreateCompactRow(parent, "Remote Media Row", 50);
            mediaPreviousButton = CreateCompactButton(mediaRow.transform, "Prev", () => FireAndForget(() => SendMediaCommandAsync("previous")), 104);
            mediaSeekBackButton = CreateCompactButton(mediaRow.transform, "-10s", () => FireAndForget(() => SendMediaCommandAsync("seek_back", 10f)), 104);
            mediaPlayPauseButton = CreateCompactButton(mediaRow.transform, "Play/Pause", () => FireAndForget(() => SendMediaCommandAsync("play_pause")), 170);
            mediaSeekForwardButton = CreateCompactButton(mediaRow.transform, "+10s", () => FireAndForget(() => SendMediaCommandAsync("seek_forward", 10f)), 104);
            mediaNextButton = CreateCompactButton(mediaRow.transform, "Next", () => FireAndForget(() => SendMediaCommandAsync("next")), 104);
            mediaFullscreenButton = CreateCompactButton(mediaRow.transform, "Full", () => FireAndForget(() => SendMediaCommandAsync("fullscreen")), 104);

            var volumeRow = CreateCompactRow(parent, "Remote Volume Row", 50);
            mediaStatusButton = CreateCompactButton(volumeRow.transform, "Player", () => FireAndForget(SendMediaStatusAsync), 130);
            mediaVolumeDownButton = CreateCompactButton(volumeRow.transform, "Vol-", () => FireAndForget(() => SendMediaCommandAsync("volume_down", 0.1f)), 118);
            mediaMuteButton = CreateCompactButton(volumeRow.transform, "Mute", () => FireAndForget(() => SendMediaCommandAsync("mute")), 118);
            mediaVolumeUpButton = CreateCompactButton(volumeRow.transform, "Vol+", () => FireAndForget(() => SendMediaCommandAsync("volume_up", 0.1f)), 118);

            remoteInputPanel = CreateCompactRow(parent, "Remote Input Row", 50);
            remoteTextInput = CreateInput(remoteInputPanel.transform, "Page input", 50, false);
            remoteTextInput.onEndEdit.AddListener(_ => RemoteInputEndEdit());
            remoteInputDoneButton = CreateCompactButton(remoteInputPanel.transform, "Done", RemoteInputDoneClicked, 110);
            remoteInputCancelButton = CreateCompactButton(remoteInputPanel.transform, "Cancel", RemoteInputCancelClicked, 130);
            remoteInputPanel.SetActive(false);

            CreateStreamPreview(parent);
        }

        private async Task RefreshServersAsync()
        {
            SetStatus("Discovering servers...");
            try
            {
                servers.Clear();
                var discovered = await client.DiscoverAsync(cancellationToken: lifetime.Token);
                servers.AddRange(discovered);
                RebuildServerDropdown();
                SetStatus(servers.Count == 0 ? "No servers found" : $"Found {servers.Count} server(s)");
            }
            catch (Exception ex)
            {
                SetStatus("Discovery failed: " + ex.Message);
            }
        }

        private async Task TryAutoConnectAsync()
        {
            var selected = FindSavedServer();
            if (selected == null)
            {
                SetStatus("Auto-connect skipped: saved server was not found");
                return;
            }

            await ConnectAsync(selected);
        }

        private DiscoveredServer FindSavedServer()
        {
            if (!string.IsNullOrEmpty(settings.ServerId))
            {
                var byId = servers.FirstOrDefault(server => server.Id == settings.ServerId);
                if (byId != null)
                {
                    return byId;
                }
            }

            if (!string.IsNullOrEmpty(settings.ServerHost))
            {
                return new DiscoveredServer
                {
                    Address = settings.ServerHost,
                    ControlPort = settings.ServerPort > 0 ? settings.ServerPort : RemoteExplorerProtocol.DefaultControlPort,
                    Id = settings.ServerId,
                    Name = "Saved server",
                    AuthMode = string.IsNullOrEmpty(settings.Password) ? "none" : "password",
                    Capabilities = new string[0]
                };
            }

            return null;
        }

        private async Task ConnectAsync(DiscoveredServer server)
        {
            SetStatus("Connecting to " + server);
            try
            {
                await client.ConnectAsync(server, passwordInput.text, lifetime.Token);
                settings.Password = passwordInput.text;
                settings.AutoConnect = autoConnectToggle.isOn;
                settings.ServerId = server.Id;
                settings.ServerHost = server.Address;
                settings.ServerPort = server.ControlPort;
                settings.Save();
                SetCommandButtons(true);
                SetStatus("Connected: " + server);
                ShowRemotePage();
                FireAndForget(StartStreamAsync);
            }
            catch (Exception ex)
            {
                SetCommandButtons(false);
                SetStatus("Connect failed: " + ex.Message);
            }
        }

        private async Task ConnectFallbackAsync(DiscoveredServer server)
        {
            var originalPassword = passwordInput != null ? passwordInput.text : null;
            try
            {
                if (passwordInput != null)
                {
                    passwordInput.text = fallbackPassword;
                }

                await client.ConnectAsync(server, fallbackPassword, lifetime.Token);
                settings.Password = fallbackPassword;
                settings.AutoConnect = autoConnectToggle != null && autoConnectToggle.isOn;
                settings.ServerId = server.Id;
                settings.ServerHost = server.Address;
                settings.ServerPort = server.ControlPort;
                settings.Save();
                SetCommandButtons(client.IsConnected);
                SetStatus("Connected: " + server);
                ShowRemotePage();
                FireAndForget(StartStreamAsync);
            }
            catch (Exception ex)
            {
                SetStatus("Connect failed: " + ex.Message);
            }
            finally
            {
                if (passwordInput != null && originalPassword != null)
                {
                    passwordInput.text = originalPassword;
                }
            }
        }

        private async Task OpenUrlAsync()
        {
            if (string.IsNullOrWhiteSpace(urlInput.text))
            {
                SetStatus("Enter a URL first");
                return;
            }

            SetStatus("Opening " + urlInput.text);
            await RunCommandAsync(() => client.NavigateAsync(urlInput.text, lifetime.Token), "Opened");
        }

        private async Task ClosePageAsync()
        {
            SetStatus("Closing current page");
            await RunCommandAsync(() => client.ClosePageAsync(lifetime.Token), "Closed page");
        }

        private async Task SendSimpleCommandAsync(string command)
        {
            SetStatus("Sending " + command);
            await RunCommandAsync(() => client.BrowserCommandAsync(command, lifetime.Token), command + " ok");
        }

        private async Task SendMediaStatusAsync()
        {
            SetStatus("Detecting page player");
            try
            {
                var result = await client.MediaStatusAsync(lifetime.Token);
                if (result.ok || result.type == "result")
                {
                    SetStatus(DescribeMediaStatus(result.result));
                    return;
                }

                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                SetStatus("Player status failed: " + error);
            }
            catch (Exception ex)
            {
                SetStatus("Player status failed: " + ex.Message);
            }
        }

        private async Task SendMediaCommandAsync(string action, float amount = 0f)
        {
            SetStatus("Media " + MediaActionLabel(action));
            try
            {
                var result = await client.MediaControlAsync(action, amount, lifetime.Token);
                if (result.ok || result.type == "result")
                {
                    SetStatus(DescribeMediaCommand(result.result, action));
                    return;
                }

                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                SetStatus("Media command failed: " + error);
            }
            catch (Exception ex)
            {
                SetStatus("Media command failed: " + ex.Message);
            }
        }

        private static string DescribeMediaStatus(WebRtcAnswer media)
        {
            if (media == null || !media.media_found)
            {
                return "No page player found";
            }

            var state = media.media_paused ? "paused" : "playing";
            var volume = Mathf.RoundToInt(Mathf.Clamp01(media.media_volume) * 100f);
            var muted = media.media_muted ? ", muted" : string.Empty;
            var fullscreen = media.media_fullscreen ? ", fullscreen" : string.Empty;
            return $"Player {state}, {FormatMediaTime(media.media_current_time)}/{FormatMediaTime(media.media_duration)}, vol {volume}%{muted}{fullscreen}";
        }

        private static string DescribeMediaCommand(WebRtcAnswer media, string fallbackAction)
        {
            if (media == null)
            {
                return "Media command sent";
            }

            if (media.controlled && media.media_reason == "keyboard_shortcut")
            {
                return $"{MediaActionLabel(string.IsNullOrEmpty(media.media_action) ? fallbackAction : media.media_action)} shortcut sent";
            }

            if (!media.media_found)
            {
                return "No page player found";
            }

            if (!media.controlled)
            {
                var reason = string.IsNullOrEmpty(media.media_reason) ? "not handled by this page" : media.media_reason;
                return $"{MediaActionLabel(fallbackAction)} unavailable: {reason}";
            }

            return $"{MediaActionLabel(string.IsNullOrEmpty(media.media_action) ? fallbackAction : media.media_action)} ok, {DescribeMediaStatus(media)}";
        }

        private static string MediaActionLabel(string action)
        {
            switch (action)
            {
                case "play_pause":
                    return "Play/Pause";
                case "fullscreen":
                    return "Fullscreen";
                case "volume_up":
                    return "Volume up";
                case "volume_down":
                    return "Volume down";
                case "mute":
                    return "Mute";
                case "seek_forward":
                    return "Seek forward";
                case "seek_back":
                    return "Seek back";
                case "next":
                    return "Next";
                case "previous":
                    return "Previous";
                default:
                    return string.IsNullOrEmpty(action) ? "command" : action;
            }
        }

        private static string FormatMediaTime(float seconds)
        {
            if (seconds <= 0f || float.IsNaN(seconds) || float.IsInfinity(seconds))
            {
                return "0:00";
            }

            var totalSeconds = Mathf.RoundToInt(seconds);
            var minutes = totalSeconds / 60;
            var remainder = totalSeconds % 60;
            return $"{minutes}:{remainder:00}";
        }

        private async Task ToggleStreamAsync()
        {
            if (IsStreamActive())
            {
                await StopStreamAsync();
            }
            else
            {
                await StartStreamAsync();
            }
        }

        private async Task StartStreamAsync()
        {
            if (streamStartInFlight)
            {
                RemoteExplorerDiagnostics.Info("Stream start ignored because another start is already in flight.");
                return;
            }

            if (!client.IsConnected)
            {
                SetStatus("Connect to a server first");
                return;
            }

            streamStartInFlight = true;
            try
            {
                var mode = SelectedStreamMode();
                var resolution = SelectedStreamResolution();
                var fps = SelectedStreamFps();
                RemoteExplorerDiagnostics.Info($"Stream start requested mode={StreamModeLabel(mode)} resolution={resolution} fps={fps}");
                // Stop the inactive transport first so both modes do not compete for server captures.
                await StopInactiveStreamTransportsAsync(mode);

                if (mode == RemoteStreamMode.WebRtc)
                {
                    await StartWebRtcStreamAsync(resolution, fps);
                    return;
                }

                await StartUdpStreamAsync(resolution, fps);
            }
            finally
            {
                streamStartInFlight = false;
            }
        }

        private async Task StartWebRtcStreamAsync(string resolution, int fps)
        {
#if REMOTE_EXPLORER_HAS_WEBRTC
            if (webRtcPlayback == null)
            {
                SetStreamStatus("WebRTC playback component is not available");
                SetStatus("WebRTC unavailable: playback component is missing");
                SetButtonLabel(streamToggleButton, "Start");
                return;
            }

            SetStatus($"Starting WebRTC {resolution} at {fps} fps");
            try
            {
                var webRtcResult = await webRtcPlayback.StartAsync(resolution, fps, lifetime.Token);
                if (webRtcResult.ok || webRtcResult.type == "result")
                {
                    SetButtonLabel(streamToggleButton, "Stop");
                    activeStreamMode = RemoteStreamMode.WebRtc;
                    hasActiveStreamMode = true;
                    ResetStreamRenderStats();
                    MarkStreamFrameClock();
                    SetStreamStatus($"WebRTC {resolution} / {fps}fps");
                    SetStatus("WebRTC stream started");
                    return;
                }

                var webRtcError = webRtcResult.error != null
                    ? $"{webRtcResult.error.code}: {webRtcResult.error.message}"
                    : "Unknown error";
                SetStreamStatus("WebRTC failed: " + webRtcError);
                SetStatus("WebRTC stream failed: " + webRtcError);
                SetButtonLabel(streamToggleButton, "Start");
            }
            catch (Exception ex)
            {
                SetStreamStatus("WebRTC failed: " + ex.Message);
                SetStatus("WebRTC stream failed: " + ex.Message);
                SetButtonLabel(streamToggleButton, "Start");
            }
#else
            SetStreamStatus("WebRTC is not available in this client build");
            SetStatus("WebRTC is not available in this client build");
            SetButtonLabel(streamToggleButton, "Start");
#endif
        }

        private async Task StartUdpStreamAsync(string resolution, int fps)
        {
            SetStatus($"Starting image stream {resolution} at {fps} fps");
            try
            {
                var result = await client.StartStreamAsync(
                    resolution,
                    fps,
                    RemoteExplorerClient.DefaultStreamQuality,
                    lifetime.Token);
                if (result.ok || result.type == "result")
                {
                    SetButtonLabel(streamToggleButton, "Stop");
                    activeStreamMode = RemoteStreamMode.UdpJpeg;
                    hasActiveStreamMode = true;
                    ResetStreamRenderStats();
                    MarkStreamFrameClock();
                    SetStreamStatus($"Waiting for {resolution} / {fps}fps frames...");
                    SetStatus("Stream started");
                    return;
                }

                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                SetStatus("Stream failed: " + error);
            }
            catch (Exception ex)
            {
                SetStatus("Stream failed: " + ex.Message);
            }
        }

        private async Task StopStreamAsync()
        {
            await StopAllStreamTransportsAsync();
            SetButtonLabel(streamToggleButton, "Start");
            SetStreamStatus("Stream stopped");
            SetStatus("Stream stopped");
            lastStreamFrameAt = -1f;
            nextStreamWatchdogAt = -1f;
            streamRestartInFlight = false;
            hasActiveStreamMode = false;
            ResetStreamRenderStats();
        }

        private async Task StopInactiveStreamTransportsAsync(RemoteStreamMode targetMode)
        {
#if REMOTE_EXPLORER_HAS_WEBRTC
            if (targetMode != RemoteStreamMode.WebRtc)
            {
                await StopWebRtcTransportAsync();
            }
#endif

            if (targetMode != RemoteStreamMode.UdpJpeg)
            {
                await StopUdpStreamTransportAsync();
            }
        }

        private async Task StopAllStreamTransportsAsync()
        {
#if REMOTE_EXPLORER_HAS_WEBRTC
            await StopWebRtcTransportAsync();
#endif
            await StopUdpStreamTransportAsync();
        }

        private async Task StopUdpStreamTransportAsync()
        {
            if (!client.IsStreaming)
            {
                return;
            }

            try
            {
                RemoteExplorerDiagnostics.Info("UDP/JPEG transport stop begin.");
                await client.StopStreamAsync(lifetime.Token);
            }
            catch (Exception ex)
            {
                Debug.LogWarning("[RemoteExplorer] Image stream stop failed: " + ex.Message);
            }
        }

#if REMOTE_EXPLORER_HAS_WEBRTC
        private async Task StopWebRtcTransportAsync()
        {
            if (webRtcPlayback == null || !webRtcPlayback.IsActive)
            {
                return;
            }

            try
            {
                RemoteExplorerDiagnostics.Info("WebRTC transport stop begin.");
                await webRtcPlayback.StopAsync(lifetime.Token);
            }
            catch (Exception ex)
            {
                Debug.LogWarning("[RemoteExplorer] WebRTC stop failed: " + ex.Message);
            }
        }
#endif

        private async Task ApplyStreamSettingsAsync()
        {
            if (!client.IsConnected || !IsStreamActive())
            {
                return;
            }

            var resolution = SelectedStreamResolution();
            var fps = SelectedStreamFps();
            var selectedMode = SelectedStreamMode();
            var activeMode = ActiveStreamModeOrSelected();

            if (selectedMode != activeMode)
            {
                RemoteExplorerDiagnostics.Info($"Stream mode change active={StreamModeLabel(activeMode)} selected={StreamModeLabel(selectedMode)}");
                SetStreamStatus($"Switching to {StreamModeLabel(selectedMode)}...");
                await StopAllStreamTransportsAsync();
                hasActiveStreamMode = false;
                lastStreamFrameAt = -1f;
                nextStreamWatchdogAt = -1f;
                ResetStreamRenderStats();
                await StartStreamAsync();
                return;
            }

#if REMOTE_EXPLORER_HAS_WEBRTC
            if (selectedMode == RemoteStreamMode.WebRtc)
            {
                SetStreamStatus($"Applying WebRTC {resolution} / {fps}fps...");
                await StopAllStreamTransportsAsync();
                hasActiveStreamMode = false;
                ResetStreamRenderStats();
                await StartStreamAsync();
                return;
            }
#endif

            SetStreamStatus($"Applying {resolution} / {fps}fps...");
            try
            {
                var result = await client.ConfigureStreamAsync(
                    resolution,
                    fps,
                    RemoteExplorerClient.DefaultStreamQuality,
                    lifetime.Token);
                if (result.ok || result.type == "result")
                {
                    SetStreamStatus($"Stream {resolution} / {fps}fps");
                    return;
                }

                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                SetStatus("Stream config failed: " + error);
            }
            catch (Exception ex)
            {
                SetStatus("Stream config failed: " + ex.Message);
            }
        }

        private void UpdateStreamWatchdog()
        {
            if (!IsStreamActive() || !client.IsConnected || streamRestartInFlight)
            {
                return;
            }

#if REMOTE_EXPLORER_HAS_WEBRTC
            // Unity WebRTC 的 OnVideoReceived 不是逐帧心跳，不能用它判断卡死。
            // Unity WebRTC OnVideoReceived is not a per-frame heartbeat, so do not watchdog it here.
            if (webRtcPlayback != null && webRtcPlayback.IsActive)
            {
                return;
            }
#endif

            if (lastStreamFrameAt < 0f || Time.unscaledTime < nextStreamWatchdogAt)
            {
                return;
            }

            if (Time.unscaledTime - lastStreamFrameAt < StreamWatchdogSeconds)
            {
                nextStreamWatchdogAt = lastStreamFrameAt + StreamWatchdogSeconds;
                return;
            }

            nextStreamWatchdogAt = Time.unscaledTime + StreamRestartCooldownSeconds;
            FireAndForget(RestartStalledStreamAsync);
        }

        private async Task RestartStalledStreamAsync()
        {
            if (streamRestartInFlight || !client.IsConnected || !IsStreamActive())
            {
                return;
            }

            streamRestartInFlight = true;
            SetStreamStatus("Stream stalled; restarting...");
            RemoteExplorerDiagnostics.Info(
                $"Stream watchdog restart last_frame_age={(Time.unscaledTime - lastStreamFrameAt):0.0}s mode={StreamModeLabel(ActiveStreamModeOrSelected())}");
            try
            {
                await StopAllStreamTransportsAsync();
                hasActiveStreamMode = false;
                ResetStreamRenderStats();
                if (!client.IsConnected)
                {
                    return;
                }

                await StartStreamAsync();
            }
            catch (Exception ex)
            {
                SetStatus("Stream restart failed: " + ex.Message);
            }
            finally
            {
                streamRestartInFlight = false;
                nextStreamWatchdogAt = Time.unscaledTime + StreamRestartCooldownSeconds;
            }
        }

        private void MarkStreamFrameClock()
        {
            lastStreamFrameAt = Time.unscaledTime;
            nextStreamWatchdogAt = lastStreamFrameAt + StreamWatchdogSeconds;
        }

        private bool IsStreamActive()
        {
#if REMOTE_EXPLORER_HAS_WEBRTC
            return client.IsStreaming || (webRtcPlayback != null && webRtcPlayback.IsActive);
#else
            return client.IsStreaming;
#endif
        }

        private RemoteStreamMode ActiveStreamModeOrSelected()
        {
#if REMOTE_EXPLORER_HAS_WEBRTC
            if (webRtcPlayback != null && webRtcPlayback.IsActive)
            {
                return RemoteStreamMode.WebRtc;
            }
#endif

            if (client.IsStreaming)
            {
                return RemoteStreamMode.UdpJpeg;
            }

            return hasActiveStreamMode ? activeStreamMode : SelectedStreamMode();
        }

        private async Task ClickPreviewAsync(Vector2 normalized)
        {
            if (!client.IsConnected)
            {
                SetStatus("Connect to a server first");
                return;
            }

            if (latestSourceWidth <= 1 || latestSourceHeight <= 1)
            {
                SetStatus("Wait for a stream frame before tapping");
                return;
            }

            var uv = streamImage != null ? streamImage.uvRect : new Rect(0f, 0f, 1f, 1f);
            var visibleX = uv.x + Mathf.Clamp01(normalized.x) * uv.width;
            var visibleY = uv.y + Mathf.Clamp01(normalized.y) * uv.height;
            var x = Mathf.Clamp01(visibleX) * latestSourceWidth;
            var y = (1f - Mathf.Clamp01(visibleY)) * latestSourceHeight;
            SetStreamStatus($"Tap {Mathf.RoundToInt(x)}, {Mathf.RoundToInt(y)}");
            RemoteExplorerDiagnostics.Info(
                $"Stream tap normalized=({normalized.x:F3},{normalized.y:F3}) uv=({visibleX:F3},{visibleY:F3}) viewport=({Mathf.RoundToInt(x)},{Mathf.RoundToInt(y)}) source={latestSourceWidth}x{latestSourceHeight}");
            try
            {
                var result = await client.ClickAsync(x, y, latestSourceWidth, latestSourceHeight, lifetime.Token);
                if (result.ok || result.type == "result")
                {
                    var click = result.result;
                    if (click != null && click.editable)
                    {
                        ShowRemoteInput(click.input_value);
                        SetStatus("Page input selected");
                        SetStreamStatus("Input focused");
                        return;
                    }

                    if (click != null && click.media_fullscreen_requested)
                    {
                        SetStatus("Video fullscreen on server");
                        return;
                    }

                    SetStatus($"Clicked {Mathf.RoundToInt(x)}, {Mathf.RoundToInt(y)}");
                    return;
                }

                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                SetStatus("Click failed: " + error);
            }
            catch (Exception ex)
            {
                SetStatus("Click failed: " + ex.Message);
            }
        }

        private async Task SwipePreviewAsync(Vector2 deltaNormalized)
        {
            if (!client.IsConnected)
            {
                SetStatus("Connect to a server first");
                return;
            }

            if (latestSourceWidth <= 1 || latestSourceHeight <= 1)
            {
                SetStatus("Wait for a stream frame before swiping");
                return;
            }

            var dx = -deltaNormalized.x * latestSourceWidth;
            var dy = deltaNormalized.y * latestSourceHeight;
            if (Mathf.Abs(dx) < 4f && Mathf.Abs(dy) < 4f)
            {
                return;
            }

            SetStreamStatus($"Scroll {Mathf.RoundToInt(dx)}, {Mathf.RoundToInt(dy)}");
            RemoteExplorerDiagnostics.Info(
                $"Stream swipe delta=({deltaNormalized.x:F3},{deltaNormalized.y:F3}) scroll=({dx:F1},{dy:F1}) source={latestSourceWidth}x{latestSourceHeight}");
            await RunCommandAsync(
                () => client.ScrollAsync(dx, dy, lifetime.Token),
                "Scrolled page");
        }

        private void ShowRemoteInput(string value)
        {
            if (remoteInputPanel == null || remoteTextInput == null)
            {
                return;
            }

            remoteInputVersion++;
            suppressRemoteInputEndEdit = true;
            remoteInputPanel.SetActive(true);
            remoteTextInput.text = value ?? string.Empty;
            remoteTextInput.caretPosition = remoteTextInput.text.Length;
            remoteTextInput.selectionAnchorPosition = remoteTextInput.text.Length;
            remoteTextInput.selectionFocusPosition = remoteTextInput.text.Length;
            Canvas.ForceUpdateCanvases();
            remoteTextInput.Select();
            remoteTextInput.ActivateInputField();
            suppressRemoteInputEndEdit = false;
        }

        private void RemoteInputEndEdit()
        {
            if (suppressRemoteInputEndEdit || remoteInputCommitInFlight || IsRemoteInputActionSelected())
            {
                return;
            }

            var version = remoteInputVersion;
            FireAndForget(() => CommitRemoteInputAsync(version));
        }

        private bool IsRemoteInputActionSelected()
        {
            var current = EventSystem.current != null ? EventSystem.current.currentSelectedGameObject : null;
            if (current == null)
            {
                return false;
            }

            return (remoteInputDoneButton != null && current == remoteInputDoneButton.gameObject) ||
                (remoteInputCancelButton != null && current == remoteInputCancelButton.gameObject);
        }

        private void RemoteInputDoneClicked()
        {
            var version = remoteInputVersion;
            FireAndForget(() => CommitRemoteInputAsync(version));
        }

        private void RemoteInputCancelClicked()
        {
            HideRemoteInput(true);
            SetStatus("Input cancelled");
        }

        private async Task CommitRemoteInputAsync(int version)
        {
            if (version != remoteInputVersion || remoteTextInput == null || remoteInputCommitInFlight)
            {
                return;
            }

            remoteInputCommitInFlight = true;
            suppressRemoteInputEndEdit = true;
            var text = remoteTextInput.text ?? string.Empty;
            SetStreamStatus("Sending input...");
            try
            {
                await RunCommandAsync(
                    () => client.SetFocusedInputAsync(text, false, lifetime.Token),
                    "Input sent");
                HideRemoteInput(false);
            }
            finally
            {
                suppressRemoteInputEndEdit = false;
                remoteInputCommitInFlight = false;
            }
        }

        private void HideRemoteInput(bool suppressEndEdit)
        {
            if (remoteInputPanel == null)
            {
                return;
            }

            var previousSuppress = suppressRemoteInputEndEdit;
            suppressRemoteInputEndEdit = suppressEndEdit || previousSuppress;
            if (remoteTextInput != null)
            {
                remoteTextInput.DeactivateInputField();
            }

            remoteInputPanel.SetActive(false);
            suppressRemoteInputEndEdit = previousSuppress;
        }

        private void PollRemoteKeyboardInput()
        {
            if (!ShouldForwardRemoteKeyboardInput())
            {
                return;
            }

            var typed = Input.inputString;
            var queuedBackspace = false;
            var queuedEnter = false;
            if (!string.IsNullOrEmpty(typed))
            {
                var textChunk = string.Empty;
                foreach (var ch in typed)
                {
                    if (ch == '\b')
                    {
                        EnqueueRemoteText(textChunk);
                        textChunk = string.Empty;
                        EnqueueRemoteKey("Backspace");
                        queuedBackspace = true;
                        continue;
                    }

                    if (ch == '\n' || ch == '\r')
                    {
                        EnqueueRemoteText(textChunk);
                        textChunk = string.Empty;
                        EnqueueRemoteKey("Enter");
                        queuedEnter = true;
                        continue;
                    }

                    if (!char.IsControl(ch))
                    {
                        textChunk += ch;
                    }
                }

                EnqueueRemoteText(textChunk);
            }

            if (!queuedBackspace && Input.GetKeyDown(KeyCode.Backspace))
            {
                EnqueueRemoteKey("Backspace");
            }

            if (!queuedEnter && (Input.GetKeyDown(KeyCode.Return) || Input.GetKeyDown(KeyCode.KeypadEnter)))
            {
                EnqueueRemoteKey("Enter");
            }

            if (Input.GetKeyDown(KeyCode.Escape))
            {
                EnqueueRemoteKey("Escape");
            }
        }

        private bool ShouldForwardRemoteKeyboardInput()
        {
            if (!client.IsConnected || remotePage == null || !remotePage.activeInHierarchy)
            {
                return false;
            }

            return !IsLocalInputFocused();
        }

        private bool IsLocalInputFocused()
        {
            var current = EventSystem.current != null ? EventSystem.current.currentSelectedGameObject : null;
            if (current == null)
            {
                return false;
            }

            var input = current.GetComponentInParent<InputField>();
            return input != null && input.isFocused;
        }

        private void EnqueueRemoteText(string text)
        {
            if (string.IsNullOrEmpty(text))
            {
                return;
            }

            EnqueueRemoteKeyboardCommand("text", text);
        }

        private void EnqueueRemoteKey(string key)
        {
            if (string.IsNullOrEmpty(key))
            {
                return;
            }

            EnqueueRemoteKeyboardCommand("key", key);
        }

        private void EnqueueRemoteKeyboardCommand(string kind, string value)
        {
            remoteKeyboardCommands.Enqueue(new RemoteKeyboardCommand
            {
                Kind = kind,
                Value = value
            });

            if (!remoteKeyboardFlushInFlight)
            {
                FireAndForget(FlushRemoteKeyboardInputAsync);
            }
        }

        private async Task FlushRemoteKeyboardInputAsync()
        {
            if (remoteKeyboardFlushInFlight)
            {
                return;
            }

            remoteKeyboardFlushInFlight = true;
            try
            {
                while (remoteKeyboardCommands.Count > 0 && client.IsConnected)
                {
                    var command = remoteKeyboardCommands.Dequeue();
                    var result = command.Kind == "key"
                        ? await client.SendKeyAsync(command.Value, lifetime.Token)
                        : await client.SendTextAsync(command.Value, lifetime.Token);
                    if (!result.ok && result.type != "result")
                    {
                        var error = result.error != null
                            ? $"{result.error.code}: {result.error.message}"
                            : "Unknown error";
                        SetStatus("Keyboard input failed: " + error);
                        remoteKeyboardCommands.Clear();
                        break;
                    }
                }
            }
            finally
            {
                remoteKeyboardFlushInFlight = false;
                if (remoteKeyboardCommands.Count > 0 && client.IsConnected)
                {
                    FireAndForget(FlushRemoteKeyboardInputAsync);
                }
            }
        }

        private async Task RunCommandAsync(Func<Task<CommandEnvelope>> action, string successMessage)
        {
            if (!client.IsConnected)
            {
                SetStatus("Connect to a server first");
                return;
            }

            try
            {
                var result = await action();
                if (result.ok || result.type == "result")
                {
                    SetStatus(successMessage);
                    return;
                }

                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                SetStatus("Command failed: " + error);
            }
            catch (Exception ex)
            {
                SetStatus("Command failed: " + ex.Message);
            }
        }

        private void ApplySettingsToUi()
        {
            if (passwordInput != null)
            {
                passwordInput.text = settings.Password ?? string.Empty;
            }

            if (autoConnectToggle != null)
            {
                autoConnectToggle.isOn = settings.AutoConnect;
            }

            if (urlInput != null)
            {
                urlInput.text = fallbackUrl;
            }

            if (streamModeDropdown != null)
            {
                streamModeDropdown.value = PreferredStreamModeIndex(settings.StreamMode);
            }
        }

        private void SaveSettings()
        {
            settings.Password = passwordInput.text;
            settings.AutoConnect = autoConnectToggle.isOn;
            settings.StreamMode = StreamModePreference(SelectedStreamMode());
            if (SelectedServer() != null)
            {
                var server = SelectedServer();
                settings.ServerId = server.Id;
                settings.ServerHost = server.Address;
                settings.ServerPort = server.ControlPort;
            }

            settings.Save();
            SetStatus("Settings saved");
        }

        private void RebuildServerDropdown()
        {
            serverDropdown.ClearOptions();
            if (servers.Count == 0)
            {
                serverDropdown.AddOptions(new List<string> { "No servers found" });
                return;
            }

            serverDropdown.AddOptions(servers.Select(server => server.ToString()).ToList());
            if (!string.IsNullOrEmpty(settings.ServerId))
            {
                var index = servers.FindIndex(server => server.Id == settings.ServerId);
                if (index >= 0)
                {
                    serverDropdown.value = index;
                }
            }
        }

        private DiscoveredServer SelectedServer()
        {
            if (servers.Count == 0 || serverDropdown.value < 0 || serverDropdown.value >= servers.Count)
            {
                return null;
            }

            return servers[serverDropdown.value];
        }

        private void RefreshButtonClicked()
        {
            FireAndForget(RefreshServersAsync);
        }

        private void ConnectButtonClicked()
        {
            var server = SelectedServer();
            if (server == null)
            {
                SetStatus("Discover a server first");
                return;
            }

            FireAndForget(() => ConnectAsync(server));
        }

        private void OpenButtonClicked()
        {
            FireAndForget(OpenUrlAsync);
        }

        private void CloseButtonClicked()
        {
            FireAndForget(ClosePageAsync);
        }

        private void ShowRemotePage()
        {
            if (connectPage != null)
            {
                connectPage.SetActive(false);
            }

            if (remotePage != null)
            {
                remotePage.SetActive(true);
            }
        }

        private void ShowConnectPage()
        {
            if (remotePage != null)
            {
                remotePage.SetActive(false);
            }

            if (connectPage != null)
            {
                connectPage.SetActive(true);
            }
        }

        private void FireAndForget(Func<Task> action)
        {
            _ = RunSafely(action);
        }

        private async Task RunSafely(Func<Task> action)
        {
            try
            {
                await action();
            }
            catch (Exception ex)
            {
                SetStatus("Error: " + ex.Message);
            }
        }

        private void SetCommandButtons(bool enabled)
        {
            SetButtonInteractable(openButton, enabled);
            SetButtonInteractable(closeButton, enabled);
            SetButtonInteractable(backButton, enabled);
            SetButtonInteractable(forwardButton, enabled);
            SetButtonInteractable(reloadButton, enabled);
            SetButtonInteractable(streamToggleButton, enabled);
            SetButtonInteractable(remoteInputDoneButton, enabled);
            SetButtonInteractable(remoteInputCancelButton, enabled);
            SetButtonInteractable(mediaStatusButton, enabled);
            SetButtonInteractable(mediaPreviousButton, enabled);
            SetButtonInteractable(mediaSeekBackButton, enabled);
            SetButtonInteractable(mediaPlayPauseButton, enabled);
            SetButtonInteractable(mediaSeekForwardButton, enabled);
            SetButtonInteractable(mediaNextButton, enabled);
            SetButtonInteractable(mediaFullscreenButton, enabled);
            SetButtonInteractable(mediaVolumeDownButton, enabled);
            SetButtonInteractable(mediaMuteButton, enabled);
            SetButtonInteractable(mediaVolumeUpButton, enabled);
        }

        private void SetStatus(string message)
        {
            currentStatus = message;
            RemoteExplorerDiagnostics.Info("Status: " + message);
            if (statusText != null)
            {
                statusText.text = message;
            }

            if (remoteStatusText != null)
            {
                remoteStatusText.text = message;
            }
        }

        private void SetStreamStatus(string message)
        {
            if (streamStatusText != null)
            {
                streamStatusText.text = message;
            }
        }

        private void ApplyStreamFrame(RemoteStreamFrame frame)
        {
            if (streamImage == null || frame == null || frame.JpegData == null || frame.JpegData.Length == 0)
            {
                return;
            }

            if (streamTexture == null)
            {
                streamTexture = new Texture2D(2, 2, TextureFormat.RGB24, false);
            }

            var decodeStartedAt = Time.realtimeSinceStartup;
            if (!streamTexture.LoadImage(frame.JpegData))
            {
                SetStreamStatus("Could not decode stream frame");
                Debug.LogWarning(
                    $"[RemoteExplorer] UDP/JPEG decode failed frame={frame.FrameId} jpeg_bytes={frame.JpegData.Length}");
                return;
            }
            var decodeMs = (Time.realtimeSinceStartup - decodeStartedAt) * 1000f;

            latestSourceWidth = Mathf.Max(1, frame.SourceWidth);
            latestSourceHeight = Mathf.Max(1, frame.SourceHeight);
            streamImage.texture = streamTexture;
            streamImage.color = Color.white;
            MarkStreamFrameClock();

            var applyStartedAt = Time.realtimeSinceStartup;
            ApplyStreamFit(frame);
            var applyMs = (Time.realtimeSinceStartup - applyStartedAt) * 1000f;
            RecordStreamRenderFrame(frame.FrameId, decodeMs, applyMs, frame.JpegData.Length, frame.Width, frame.Height);

            if (Time.unscaledTime >= nextStreamStatusAt)
            {
                nextStreamStatusAt = Time.unscaledTime + StreamStatusIntervalSeconds;
                SetStreamStatus(
                    $"Stream #{frame.FrameId} {frame.Width}x{frame.Height} | source {latestSourceWidth}x{latestSourceHeight}");
            }
        }

        private void ResetStreamRenderStats()
        {
            streamRenderStatsSince = Time.unscaledTime;
            nextStreamRenderLogAt = Time.unscaledTime + StreamRenderLogIntervalSeconds;
            streamRenderFrames = 0;
            streamRenderFrameGaps = 0;
            streamRenderDecodeMsTotal = 0f;
            streamRenderDecodeMsMax = 0f;
            streamRenderApplyMsTotal = 0f;
            streamRenderApplyMsMax = 0f;
            streamRenderLastFrameId = 0;
            streamRenderHasFrameId = false;
        }

        private void RecordStreamRenderFrame(
            uint frameId,
            float decodeMs,
            float applyMs,
            int jpegBytes,
            int width,
            int height)
        {
            if (streamRenderStatsSince < 0f)
            {
                ResetStreamRenderStats();
            }

            if (streamRenderHasFrameId && IsFrameIdNewer(frameId, streamRenderLastFrameId))
            {
                var delta = unchecked((int)(frameId - streamRenderLastFrameId));
                if (delta > 1)
                {
                    streamRenderFrameGaps += delta - 1;
                }
            }

            streamRenderFrames++;
            streamRenderLastFrameId = frameId;
            streamRenderHasFrameId = true;
            streamRenderDecodeMsTotal += decodeMs;
            streamRenderDecodeMsMax = Mathf.Max(streamRenderDecodeMsMax, decodeMs);
            streamRenderApplyMsTotal += applyMs;
            streamRenderApplyMsMax = Mathf.Max(streamRenderApplyMsMax, applyMs);

            if (Time.unscaledTime < nextStreamRenderLogAt)
            {
                return;
            }

            var elapsed = Mathf.Max(0.001f, Time.unscaledTime - streamRenderStatsSince);
            RemoteExplorerDiagnostics.Info(
                "UDP/JPEG render stats " +
                $"elapsed={elapsed:0.0}s frames={streamRenderFrames} fps={(streamRenderFrames / elapsed):0.0} " +
                $"frame_gaps={streamRenderFrameGaps} decode_ms={(streamRenderDecodeMsTotal / Mathf.Max(1, streamRenderFrames)):0.0}/{streamRenderDecodeMsMax:0.0} " +
                $"apply_ms={(streamRenderApplyMsTotal / Mathf.Max(1, streamRenderFrames)):0.0}/{streamRenderApplyMsMax:0.0} " +
                $"last_frame={frameId} size={width}x{height} jpeg_bytes={jpegBytes}");

            streamRenderStatsSince = Time.unscaledTime;
            nextStreamRenderLogAt = Time.unscaledTime + StreamRenderLogIntervalSeconds;
            streamRenderFrames = 0;
            streamRenderFrameGaps = 0;
            streamRenderDecodeMsTotal = 0f;
            streamRenderDecodeMsMax = 0f;
            streamRenderApplyMsTotal = 0f;
            streamRenderApplyMsMax = 0f;
        }

        private void ApplyStreamFit(RemoteStreamFrame frame)
        {
            if (streamImage == null || frame == null || frame.Width <= 0 || frame.Height <= 0)
            {
                return;
            }

            ApplyStreamFit(frame.Width, frame.Height);
        }

        private void ApplyStreamFit(int width, int height)
        {
            if (streamImage == null || width <= 0 || height <= 0)
            {
                return;
            }

            streamImage.uvRect = new Rect(0f, 0f, 1f, 1f);

            var imageRect = streamImage.rectTransform;
            var parentRect = imageRect.parent as RectTransform;
            var bounds = parentRect != null ? parentRect.rect : imageRect.rect;
            if (bounds.width <= 1f || bounds.height <= 1f)
            {
                return;
            }

            var imageAspect = (float)width / height;
            var boundsAspect = bounds.width / bounds.height;
            var fittedWidth = bounds.width;
            var fittedHeight = fittedWidth / imageAspect;
            if (fittedHeight > bounds.height)
            {
                fittedHeight = bounds.height;
                fittedWidth = fittedHeight * imageAspect;
            }

            if (boundsAspect > imageAspect)
            {
                fittedWidth = Mathf.Min(fittedWidth, bounds.height * imageAspect);
            }

            imageRect.anchorMin = new Vector2(0.5f, 0.5f);
            imageRect.anchorMax = new Vector2(0.5f, 0.5f);
            imageRect.pivot = new Vector2(0.5f, 0.5f);
            imageRect.anchoredPosition = Vector2.zero;
            imageRect.sizeDelta = new Vector2(fittedWidth, fittedHeight);
        }

        private static void EnsureEventSystem()
        {
            if (FindObjectOfType<EventSystem>() != null)
            {
                return;
            }

            var eventSystem = new GameObject("EventSystem");
            eventSystem.AddComponent<EventSystem>();
            eventSystem.AddComponent<StandaloneInputModule>();
        }

        private static GameObject CreatePanel(Transform parent, string name, Color color)
        {
            var panel = CreateUiObject(name, parent);
            var image = panel.AddComponent<Image>();
            image.color = color;
            return panel;
        }

        private static GameObject CreatePage(
            Transform parent,
            string name,
            int horizontalPadding,
            int verticalPadding,
            float spacing)
        {
            var page = CreatePanel(parent, name, new Color(0.06f, 0.07f, 0.09f, 1f));
            var element = page.AddComponent<LayoutElement>();
            element.flexibleHeight = 1;
            element.preferredHeight = 0;

            var layout = page.AddComponent<VerticalLayoutGroup>();
            layout.padding = new RectOffset(horizontalPadding, horizontalPadding, verticalPadding, verticalPadding);
            layout.spacing = spacing;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = true;
            layout.childForceExpandHeight = false;
            return page;
        }

        private static GameObject CreateRow(Transform parent, string name, float height)
        {
            return CreateRow(parent, name, height, true);
        }

        private static GameObject CreateCompactRow(Transform parent, string name, float height)
        {
            return CreateRow(parent, name, height, false);
        }

        private static GameObject CreateRow(Transform parent, string name, float height, bool forceExpandWidth)
        {
            var row = CreateUiObject(name, parent);
            var rect = row.GetComponent<RectTransform>();
            rect.sizeDelta = new Vector2(0, height);
            var layout = row.AddComponent<HorizontalLayoutGroup>();
            layout.spacing = 14;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = forceExpandWidth;
            layout.childForceExpandHeight = true;
            var element = row.AddComponent<LayoutElement>();
            element.minHeight = height;
            element.preferredHeight = height;
            return row;
        }

        private static Text CreateText(
            Transform parent,
            string value,
            int size,
            FontStyle style,
            TextAnchor alignment,
            float height)
        {
            var textObject = CreateUiObject("Text", parent);
            var text = textObject.AddComponent<Text>();
            text.text = value;
            text.font = BuiltinFont();
            text.fontSize = size;
            text.fontStyle = style;
            text.alignment = alignment;
            text.color = Color.white;
            text.horizontalOverflow = HorizontalWrapMode.Wrap;
            text.verticalOverflow = VerticalWrapMode.Truncate;
            var element = textObject.AddComponent<LayoutElement>();
            element.minHeight = height;
            element.preferredHeight = height;
            return text;
        }

        private static InputField CreateInput(Transform parent, string placeholder, float height, bool password)
        {
            var root = CreatePanel(parent, "Input", new Color(0.12f, 0.14f, 0.18f, 1f));
            var layout = root.AddComponent<LayoutElement>();
            layout.minHeight = height;
            layout.preferredHeight = height;
            layout.flexibleWidth = 1;
            var field = root.AddComponent<InputField>();
            field.contentType = password ? InputField.ContentType.Password : InputField.ContentType.Standard;

            var text = CreateText(root.transform, string.Empty, 26, FontStyle.Normal, TextAnchor.MiddleLeft, height);
            Stretch(text.rectTransform, 20, 20, 0, 0);
            field.textComponent = text;

            var placeholderText = CreateText(root.transform, placeholder, 26, FontStyle.Italic, TextAnchor.MiddleLeft, height);
            placeholderText.color = new Color(0.72f, 0.76f, 0.82f, 1f);
            Stretch(placeholderText.rectTransform, 20, 20, 0, 0);
            field.placeholder = placeholderText;

            return field;
        }

        private static Button CreateButton(Transform parent, string label, Action onClick, float width)
        {
            var root = CreatePanel(parent, "Button " + label, new Color(0.18f, 0.35f, 0.72f, 1f));
            root.AddComponent<LayoutElement>().preferredWidth = width;
            var button = root.AddComponent<Button>();
            button.onClick.AddListener(() => onClick());
            var colors = button.colors;
            colors.highlightedColor = new Color(0.26f, 0.46f, 0.9f, 1f);
            colors.pressedColor = new Color(0.12f, 0.24f, 0.52f, 1f);
            colors.disabledColor = new Color(0.22f, 0.24f, 0.28f, 0.7f);
            button.colors = colors;

            var text = CreateText(root.transform, label, 24, FontStyle.Bold, TextAnchor.MiddleCenter, 0);
            Stretch(text.rectTransform, 8, 8, 0, 0);
            return button;
        }

        private static Button CreateCompactButton(Transform parent, string label, Action onClick, float width)
        {
            var button = CreateButton(parent, label, onClick, width);
            var text = button.GetComponentInChildren<Text>();
            if (text != null)
            {
                text.fontSize = 24;
                text.resizeTextForBestFit = true;
                text.resizeTextMinSize = 18;
                text.resizeTextMaxSize = 24;
            }

            return button;
        }

        private static Dropdown CreateDropdown(Transform parent)
        {
            var root = CreatePanel(parent, "Dropdown", new Color(0.12f, 0.14f, 0.18f, 1f));
            var dropdown = root.AddComponent<Dropdown>();
            dropdown.targetGraphic = root.GetComponent<Image>();

            var label = CreateText(root.transform, string.Empty, 18, FontStyle.Normal, TextAnchor.MiddleLeft, 0);
            label.horizontalOverflow = HorizontalWrapMode.Overflow;
            label.verticalOverflow = VerticalWrapMode.Truncate;
            label.resizeTextForBestFit = true;
            label.resizeTextMinSize = 12;
            label.resizeTextMaxSize = 18;
            Stretch(label.rectTransform, 14, 34, 0, 0);
            dropdown.captionText = label;

            var arrow = CreateText(root.transform, "v", 16, FontStyle.Bold, TextAnchor.MiddleCenter, 0);
            Anchor(arrow.rectTransform, new Vector2(1, 0.5f), new Vector2(1, 0.5f), new Vector2(-17, 0), new Vector2(28, 28));

            var template = CreateDropdownTemplate(root.transform);
            dropdown.template = template;
            dropdown.itemText = template.GetComponentInChildren<Toggle>(true).GetComponentInChildren<Text>(true);
            dropdown.options = new List<Dropdown.OptionData> { new Dropdown.OptionData("No servers found") };
            return dropdown;
        }

        private static RectTransform CreateDropdownTemplate(Transform parent)
        {
            var template = CreatePanel(parent, "Template", new Color(0.1f, 0.11f, 0.14f, 1f));
            template.SetActive(false);
            var rect = template.GetComponent<RectTransform>();
            rect.pivot = new Vector2(0.5f, 1f);
            Anchor(rect, new Vector2(0, 0), new Vector2(1, 0), new Vector2(0, -4), new Vector2(0, 220));

            var scroll = template.AddComponent<ScrollRect>();
            scroll.horizontal = false;
            scroll.vertical = true;
            scroll.movementType = ScrollRect.MovementType.Clamped;
            scroll.scrollSensitivity = 18f;

            var viewport = CreatePanel(template.transform, "Viewport", new Color(0.1f, 0.11f, 0.14f, 1f));
            Stretch(viewport.GetComponent<RectTransform>(), 0, 0, 0, 0);
            var viewportRect = viewport.GetComponent<RectTransform>();
            viewport.AddComponent<RectMask2D>();
            scroll.viewport = viewportRect;

            var content = CreateUiObject("Content", viewport.transform);
            var contentRect = content.GetComponent<RectTransform>();
            contentRect.anchorMin = new Vector2(0, 1);
            contentRect.anchorMax = new Vector2(1, 1);
            contentRect.pivot = new Vector2(0.5f, 1);
            contentRect.anchoredPosition = Vector2.zero;
            contentRect.sizeDelta = Vector2.zero;
            scroll.content = contentRect;

            var contentLayout = content.AddComponent<VerticalLayoutGroup>();
            contentLayout.childControlHeight = true;
            contentLayout.childControlWidth = true;
            contentLayout.childForceExpandHeight = false;
            contentLayout.childForceExpandWidth = true;
            var contentFitter = content.AddComponent<ContentSizeFitter>();
            contentFitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;

            var item = CreatePanel(content.transform, "Item", new Color(0.14f, 0.16f, 0.2f, 1f));
            var itemLayout = item.AddComponent<LayoutElement>();
            itemLayout.minHeight = 42;
            itemLayout.preferredHeight = 42;
            var toggle = item.AddComponent<Toggle>();
            var itemText = CreateText(item.transform, "Option", 22, FontStyle.Normal, TextAnchor.MiddleLeft, 0);
            itemText.fontSize = 17;
            itemText.resizeTextForBestFit = true;
            itemText.resizeTextMinSize = 11;
            itemText.resizeTextMaxSize = 17;
            itemText.horizontalOverflow = HorizontalWrapMode.Overflow;
            Stretch(itemText.rectTransform, 12, 12, 0, 0);
            toggle.targetGraphic = item.GetComponent<Image>();

            var dropdown = parent.GetComponent<Dropdown>();
            if (dropdown != null)
            {
                dropdown.itemText = itemText;
            }

            return rect;
        }

        private static Toggle CreateToggle(Transform parent, string label, float height)
        {
            var row = CreateRow(parent, "Toggle Row", height);
            var box = CreatePanel(row.transform, "Toggle Box", new Color(0.14f, 0.16f, 0.2f, 1f));
            box.AddComponent<LayoutElement>().preferredWidth = 64;
            var toggle = box.AddComponent<Toggle>();
            toggle.targetGraphic = box.GetComponent<Image>();
            var check = CreatePanel(box.transform, "Checkmark", new Color(0.24f, 0.72f, 0.42f, 1f));
            Stretch(check.GetComponent<RectTransform>(), 14, 14, 14, 14);
            toggle.graphic = check.GetComponent<Image>();

            CreateText(row.transform, label, 24, FontStyle.Normal, TextAnchor.MiddleLeft, height);
            return toggle;
        }

        private static Slider CreateSlider(Transform parent, float minValue, float maxValue, float value, float width)
        {
            var root = CreatePanel(parent, "Slider", new Color(0.1f, 0.12f, 0.15f, 1f));
            var rootLayout = root.AddComponent<LayoutElement>();
            rootLayout.preferredWidth = width;
            rootLayout.flexibleWidth = 1;

            var slider = root.AddComponent<Slider>();
            slider.minValue = minValue;
            slider.maxValue = maxValue;
            slider.value = value;
            slider.wholeNumbers = true;

            var background = CreatePanel(root.transform, "Background", new Color(0.18f, 0.2f, 0.24f, 1f));
            Anchor(background.GetComponent<RectTransform>(), new Vector2(0, 0.5f), new Vector2(1, 0.5f), Vector2.zero, new Vector2(-34, 10));

            var fillArea = CreateUiObject("Fill Area", root.transform);
            Stretch(fillArea.GetComponent<RectTransform>(), 18, 18, 0, 0);
            var fill = CreatePanel(fillArea.transform, "Fill", new Color(0.24f, 0.72f, 0.42f, 1f));
            Stretch(fill.GetComponent<RectTransform>(), 0, 0, 20, 20);

            var handleArea = CreateUiObject("Handle Slide Area", root.transform);
            Stretch(handleArea.GetComponent<RectTransform>(), 18, 18, 0, 0);
            var handle = CreatePanel(handleArea.transform, "Handle", new Color(0.88f, 0.92f, 0.98f, 1f));
            Anchor(handle.GetComponent<RectTransform>(), new Vector2(0, 0.5f), new Vector2(0, 0.5f), Vector2.zero, new Vector2(34, 34));

            slider.fillRect = fill.GetComponent<RectTransform>();
            slider.handleRect = handle.GetComponent<RectTransform>();
            slider.targetGraphic = handle.GetComponent<Image>();
            return slider;
        }

        private void CreateStreamPreview(Transform parent)
        {
            var previewPanel = CreatePanel(parent, "Stream Preview", new Color(0.02f, 0.025f, 0.03f, 1f));
            var panelImage = previewPanel.GetComponent<Image>();
            if (panelImage != null)
            {
                panelImage.raycastTarget = false;
            }

            var previewLayout = previewPanel.AddComponent<LayoutElement>();
            previewLayout.flexibleHeight = 1;
            previewLayout.minHeight = RemotePreviewMinHeight;
            previewLayout.preferredHeight = RemotePreviewPreferredHeight;

            var previewObject = CreateUiObject("Stream Image", previewPanel.transform);
            Stretch(previewObject.GetComponent<RectTransform>(), 0, 0, 0, 0);
            streamImage = previewObject.AddComponent<RawImage>();
            streamImage.color = new Color(0.12f, 0.14f, 0.18f, 1f);
            streamImage.raycastTarget = true;
            streamImage.uvRect = new Rect(0f, 0f, 1f, 1f);

            var streamView = previewObject.AddComponent<RemoteExplorerStreamView>();
            streamView.OnTap = point => FireAndForget(() => ClickPreviewAsync(point));
            streamView.OnSwipe = delta => FireAndForget(() => SwipePreviewAsync(delta));

#if REMOTE_EXPLORER_HAS_WEBRTC
            webRtcPlayback = gameObject.GetComponent<RemoteExplorerWebRtcPlayback>();
            if (webRtcPlayback == null)
            {
                webRtcPlayback = gameObject.AddComponent<RemoteExplorerWebRtcPlayback>();
            }

            webRtcPlayback.Configure(client, streamImage, SetStreamStatus);
            webRtcPlayback.OnFrameSize = (width, height) =>
            {
                latestSourceWidth = Mathf.Max(1, width);
                latestSourceHeight = Mathf.Max(1, height);
                ApplyStreamFit(latestSourceWidth, latestSourceHeight);
            };
            webRtcPlayback.OnFrameReceived = MarkStreamFrameClock;
#endif
        }

        private string SelectedStreamResolution()
        {
            if (streamResolutionDropdown == null || streamResolutionDropdown.options.Count == 0)
            {
                return "360p";
            }

            var index = Mathf.Clamp(streamResolutionDropdown.value, 0, streamResolutionDropdown.options.Count - 1);
            return streamResolutionDropdown.options[index].text;
        }

        private RemoteStreamMode SelectedStreamMode()
        {
#if REMOTE_EXPLORER_HAS_WEBRTC
            if (streamModeDropdown == null || streamModeDropdown.options.Count == 0)
            {
                return StreamModeFromPreference(settings.StreamMode);
            }

            var index = Mathf.Clamp(streamModeDropdown.value, 0, streamModeDropdown.options.Count - 1);
            var label = streamModeDropdown.options[index].text ?? string.Empty;
            return label.StartsWith("UDP", StringComparison.OrdinalIgnoreCase)
                ? RemoteStreamMode.UdpJpeg
                : RemoteStreamMode.WebRtc;
#else
            return RemoteStreamMode.UdpJpeg;
#endif
        }

        private static int PreferredStreamModeIndex(string mode)
        {
#if REMOTE_EXPLORER_HAS_WEBRTC
            return StreamModeFromPreference(mode) == RemoteStreamMode.UdpJpeg ? 1 : 0;
#else
            return 0;
#endif
        }

        private static RemoteStreamMode StreamModeFromPreference(string mode)
        {
#if REMOTE_EXPLORER_HAS_WEBRTC
            return string.Equals(mode, "udp", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(mode, "udp_jpeg", StringComparison.OrdinalIgnoreCase)
                ? RemoteStreamMode.UdpJpeg
                : RemoteStreamMode.WebRtc;
#else
            return RemoteStreamMode.UdpJpeg;
#endif
        }

        private static string StreamModePreference(RemoteStreamMode mode)
        {
            return mode == RemoteStreamMode.WebRtc ? "webrtc" : "udp";
        }

        private static string StreamModeLabel(RemoteStreamMode mode)
        {
            return mode == RemoteStreamMode.WebRtc ? "WebRTC" : "UDP/JPEG";
        }

        private static bool IsFrameIdNewer(uint candidate, uint baseline)
        {
            return candidate != baseline && unchecked((int)(candidate - baseline)) > 0;
        }

        private int SelectedStreamFps()
        {
            if (streamFpsSlider == null)
            {
                return RemoteExplorerClient.DefaultStreamFps;
            }

            var value = Mathf.RoundToInt(streamFpsSlider.value);
            return Mathf.Clamp(value, RemoteExplorerClient.MinStreamFps, RemoteExplorerClient.MaxStreamFps);
        }

        private void UpdateStreamFpsLabel()
        {
            if (streamFpsText != null)
            {
                streamFpsText.text = "FPS " + SelectedStreamFps();
            }
        }

        private void ScheduleStreamSettingsApply()
        {
            UpdateStreamFpsLabel();
            settings.StreamMode = StreamModePreference(SelectedStreamMode());
            settings.Save();
            if (!IsStreamActive())
            {
                return;
            }

            streamSettingsDirty = true;
            streamSettingsApplyAt = Time.unscaledTime + 0.45f;
        }

        private static void SetButtonLabel(Button button, string label)
        {
            if (button == null)
            {
                return;
            }

            var text = button.GetComponentInChildren<Text>();
            if (text != null)
            {
                text.text = label;
            }
        }

        private static void SetButtonInteractable(Button button, bool enabled)
        {
            if (button != null)
            {
                button.interactable = enabled;
            }
        }

        private static void Stretch(RectTransform rect, float left, float right, float top, float bottom)
        {
            if (rect == null)
            {
                return;
            }

            rect.anchorMin = Vector2.zero;
            rect.anchorMax = Vector2.one;
            rect.offsetMin = new Vector2(left, bottom);
            rect.offsetMax = new Vector2(-right, -top);
        }

        private static void Anchor(RectTransform rect, Vector2 min, Vector2 max, Vector2 position, Vector2 size)
        {
            if (rect == null)
            {
                return;
            }

            rect.anchorMin = min;
            rect.anchorMax = max;
            rect.anchoredPosition = position;
            rect.sizeDelta = size;
        }

        private static GameObject CreateUiObject(string name, Transform parent)
        {
            var go = new GameObject(name, typeof(RectTransform));
            if (parent != null)
            {
                go.transform.SetParent(parent, false);
            }

            return go;
        }

        private static Font BuiltinFont()
        {
            var font = TryBuiltinFont("LegacyRuntime.ttf");
            if (font == null)
            {
                font = TryBuiltinFont("Arial.ttf");
            }

            return font;
        }

        private static Font TryBuiltinFont(string fontName)
        {
            try
            {
                return Resources.GetBuiltinResource<Font>(fontName);
            }
            catch (ArgumentException)
            {
                return null;
            }
        }
    }
}
