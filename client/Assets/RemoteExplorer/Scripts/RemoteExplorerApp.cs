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
        private const float ConnectionHealthIntervalSeconds = 5f;
        private const float ConnectionHealthTimeoutSeconds = 1.5f;
        private const int StreamStatusLogLimit = 80;

        private readonly RemoteExplorerClient client = new RemoteExplorerClient();
        private readonly List<DiscoveredServer> servers = new List<DiscoveredServer>();
        private RemoteExplorerSettings settings;
        private CancellationTokenSource lifetime;

        private Dropdown serverDropdown;
        private InputField serverNameInput;
        private InputField serverAddressInput;
        private InputField passwordInput;
        private Toggle savePasswordToggle;
        private Toggle defaultServerToggle;
        private Toggle autoConnectToggle;
        private InputField urlInput;
        private InputField customFavoriteInput;
        private Text statusText;
        private Text remoteStatusText;
        private Text remoteSubtitleText;
        private Text streamBadgeText;
        private GameObject connectPage;
        private GameObject remotePage;
        private Button connectButton;
        private Button discoverButton;
        private Button addServerButton;
        private Button deleteServerButton;
        private Button openButton;
        private Button closeButton;
        private Button favoritesButton;
        private Button addFavoriteButton;
        private Button deleteFavoriteButton;
        private Button defaultUrlButton;
        private Button cacheButton;
        private Button settingsButton;
        private Button exitButton;
        private Button streamFullscreenButton;
        private Button confirmClearCacheButton;
        private Button backButton;
        private Button forwardButton;
        private Button reloadButton;
        private GameObject remoteInputPanel;
        private InputField remoteTextInput;
        private Button remoteInputDoneButton;
        private Button remoteInputCancelButton;
        private Button mediaPreviousButton;
        private Button mediaSeekBackButton;
        private Button mediaPlayPauseButton;
        private Button mediaSeekForwardButton;
        private Button mediaNextButton;
        private Button mediaFullscreenButton;
        private Button mediaExitFullscreenButton;
        private Button mediaVolumeDownButton;
        private Button mediaMuteButton;
        private Button mediaVolumeUpButton;
        private readonly Queue<RemoteKeyboardCommand> remoteKeyboardCommands = new Queue<RemoteKeyboardCommand>();
        private Dropdown streamModeDropdown;
        private Dropdown streamResolutionDropdown;
        private Slider streamFpsSlider;
        private Text streamFpsText;
        private Button streamToggleButton;
        private Button streamLogButton;
        private GameObject streamPreviewPanel;
        private GameObject streamFullscreenOverlay;
        private GameObject streamFullscreenHost;
        private Transform streamImageOriginalParent;
        private int streamImageOriginalSiblingIndex;
        private bool streamPreviewFullscreen;
        private RawImage streamImage;
        private GameObject serverRowsContainer;
        private GameObject favoritesModal;
        private GameObject streamSettingsModal;
        private GameObject clearCacheModal;
        private GameObject streamLogModal;
        private Text streamLogText;
        private Transform favoriteButtonsContainer;
        private Transform resolutionButtonsContainer;
        private Toggle clearCookiesToggle;
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
        private bool connectionHealthCheckInFlight;
        private int remoteInputVersion;
        private float nextConnectionHealthCheckAt = -1f;
        private bool canvasReady;
        private string fallbackPassword = string.Empty;
        private string fallbackUrl = "https://www.youtube.com";
        private string currentStatus = "Starting...";
        private string fatalUiError;
        private int instanceId;
        private int selectedSavedServerIndex = -1;
        private string selectedFavoriteUrl = string.Empty;
        private readonly List<Button> serverRowButtons = new List<Button>();
        private readonly List<Button> resolutionButtons = new List<Button>();
        private readonly List<string> streamStatusLog = new List<string>();

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

            client.ConnectionLost += HandleConnectionLost;
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
            client.ConnectionLost -= HandleConnectionLost;
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
            UpdateConnectionHealth();
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
                ? new Vector2(1440, 920)
                : new Vector2(1080, 1920);
            scaler.matchWidthOrHeight = 0.5f;

            var root = CreatePanel(canvasObject.transform, "Root", Theme.Background);
            Stretch(root.GetComponent<RectTransform>(), 0, 0, 0, 0);
            var layout = root.AddComponent<VerticalLayoutGroup>();
            layout.padding = new RectOffset(0, 0, 0, 0);
            layout.spacing = 0;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = true;
            layout.childForceExpandHeight = true;

            connectPage = CreatePage(root.transform, "Connect Page", 32, 32, 24);
            BuildConnectPage(connectPage.transform);

            remotePage = CreatePage(root.transform, "Remote Page", 32, 32, 24);
            BuildRemotePage(remotePage.transform);
            remotePage.SetActive(false);

            SetCommandButtons(false);
        }

        private void BuildConnectPage(Transform parent)
        {
            CreateBrandHeader(parent, "客户端主页", "Remote TV-like Controller", "搜索局域网服务器", RefreshButtonClicked, out discoverButton);
            statusText = CreateText(parent, "打开时自动加载已保存服务器；点击搜索会扫描局域网内可用 RCViewer 服务端。", 14, FontStyle.Normal, TextAnchor.MiddleLeft, 24);
            statusText.color = Theme.MutedText;

            var body = CreateUiObject("Client Home Body", parent);
            var bodyLayout = body.AddComponent<HorizontalLayoutGroup>();
            bodyLayout.spacing = 24;
            bodyLayout.childControlWidth = true;
            bodyLayout.childControlHeight = true;
            bodyLayout.childForceExpandWidth = false;
            bodyLayout.childForceExpandHeight = true;
            var bodyElement = body.AddComponent<LayoutElement>();
            bodyElement.flexibleHeight = 1;
            bodyElement.minHeight = 520;

            var sidebar = CreateCard(body.transform, "Server Management Sidebar");
            var sidebarElement = sidebar.AddComponent<LayoutElement>();
            sidebarElement.preferredWidth = 320;
            sidebarElement.minWidth = 300;
            sidebarElement.flexibleWidth = 0;
            var sidebarLayout = sidebar.AddComponent<VerticalLayoutGroup>();
            sidebarLayout.padding = new RectOffset(24, 24, 22, 24);
            sidebarLayout.spacing = 12;
            sidebarLayout.childControlWidth = true;
            sidebarLayout.childControlHeight = true;
            sidebarLayout.childForceExpandWidth = true;
            sidebarLayout.childForceExpandHeight = false;

            CreateCardTitle(sidebar.transform, "服务端管理", 28);
            CreateLabel(sidebar.transform, "服务器名称");
            serverNameInput = CreateInput(sidebar.transform, "客厅电脑", 42, false);
            CreateLabel(sidebar.transform, "地址");
            serverAddressInput = CreateInput(sidebar.transform, "192.168.1.20:45454", 42, false);
            CreateLabel(sidebar.transform, "连接密码");
            passwordInput = CreateInput(sidebar.transform, "可留空", 42, true);
            savePasswordToggle = CreateToggle(sidebar.transform, "保存密码", 38);
            defaultServerToggle = CreateToggle(sidebar.transform, "设为默认服务器", 38);
            autoConnectToggle = defaultServerToggle;

            var serverButtonRow = CreateCompactRow(sidebar.transform, "Server Form Buttons", 44);
            addServerButton = CreatePrimaryButton(serverButtonRow.transform, "添加/保存", SaveManagedServer, 128);
            deleteServerButton = CreateSecondaryButton(serverButtonRow.transform, "删除", DeleteManagedServer, 90);

            var listCard = CreateCard(body.transform, "Server List Card");
            listCard.AddComponent<LayoutElement>().flexibleWidth = 1;
            var listLayout = listCard.AddComponent<VerticalLayoutGroup>();
            listLayout.padding = new RectOffset(24, 24, 22, 24);
            listLayout.spacing = 16;
            listLayout.childControlWidth = true;
            listLayout.childControlHeight = true;
            listLayout.childForceExpandWidth = true;
            listLayout.childForceExpandHeight = false;

            CreateCardTitle(listCard.transform, "服务器列表", 32);
            serverRowsContainer = CreateUiObject("Server Rows", listCard.transform);
            var rowsLayout = serverRowsContainer.AddComponent<VerticalLayoutGroup>();
            rowsLayout.spacing = 14;
            rowsLayout.childControlWidth = true;
            rowsLayout.childControlHeight = true;
            rowsLayout.childForceExpandWidth = true;
            rowsLayout.childForceExpandHeight = false;
            var rowsElement = serverRowsContainer.AddComponent<LayoutElement>();
            rowsElement.preferredHeight = 320;
            rowsElement.minHeight = 0;
            rowsElement.flexibleHeight = 0;

            var footerSpacer = CreateUiObject("Server List Footer Spacer", listCard.transform);
            footerSpacer.AddComponent<LayoutElement>().flexibleHeight = 1;

            var note = CreateText(listCard.transform, "已保存服务器会在下次打开时自动加载；局域网搜索结果可直接连接，也可以添加到列表。", 14, FontStyle.Normal, TextAnchor.LowerLeft, 56);
            note.color = Theme.MutedText;

            serverDropdown = CreateDropdown(listCard.transform);
            serverDropdown.gameObject.SetActive(false);
        }

        private void BuildRemotePage(Transform parent)
        {
            var headerRow = CreateUiObject("Remote Header", parent);
            var headerLayout = headerRow.AddComponent<HorizontalLayoutGroup>();
            headerLayout.spacing = 16;
            headerLayout.childControlWidth = true;
            headerLayout.childControlHeight = true;
            headerLayout.childForceExpandWidth = false;
            headerLayout.childForceExpandHeight = false;
            headerRow.AddComponent<LayoutElement>().preferredHeight = 74;

            var logo = CreateLogo(headerRow.transform);
            ApplyLogoLayout(logo);
            var titleColumn = CreateUiObject("Remote Title Column", headerRow.transform);
            titleColumn.AddComponent<LayoutElement>().flexibleWidth = 1;
            var titleLayout = titleColumn.AddComponent<VerticalLayoutGroup>();
            titleLayout.spacing = 0;
            titleLayout.childControlWidth = true;
            titleLayout.childControlHeight = true;
            titleLayout.childForceExpandWidth = true;
            titleLayout.childForceExpandHeight = false;
            CreateText(titleColumn.transform, "RCViewer", 22, FontStyle.Bold, TextAnchor.MiddleLeft, 28).color = Theme.PrimaryText;
            remoteSubtitleText = CreateText(titleColumn.transform, "已连接：-", 12, FontStyle.Normal, TextAnchor.MiddleLeft, 20);
            remoteSubtitleText.color = Theme.MutedText;

            streamBadgeText = CreatePill(headerRow.transform, "串流关闭", Theme.SuccessText, 112);
            exitButton = CreateSecondaryButton(headerRow.transform, "退出", ExitRemoteSessionClicked, 96);

            CreateText(parent, "远程控制", 30, FontStyle.Bold, TextAnchor.MiddleLeft, 40).color = Theme.PrimaryText;

            var urlCard = CreateCard(parent, "Favorites And URL Bar");
            urlCard.AddComponent<LayoutElement>().preferredHeight = 76;
            var urlLayout = urlCard.AddComponent<HorizontalLayoutGroup>();
            urlLayout.padding = new RectOffset(22, 22, 16, 16);
            urlLayout.spacing = 14;
            urlLayout.childControlWidth = true;
            urlLayout.childControlHeight = true;
            urlLayout.childForceExpandWidth = false;
            urlLayout.childForceExpandHeight = false;

            favoritesButton = CreateSecondaryButton(urlCard.transform, "收藏夹", ToggleFavoritesModal, 96);
            var urlColumn = CreateUiObject("URL Column", urlCard.transform);
            var urlColumnElement = urlColumn.AddComponent<LayoutElement>();
            urlColumnElement.flexibleWidth = 1;
            urlColumnElement.minWidth = 360;
            var urlColumnLayout = urlColumn.AddComponent<VerticalLayoutGroup>();
            urlColumnLayout.spacing = 6;
            urlColumnLayout.childControlWidth = true;
            urlColumnLayout.childControlHeight = true;
            urlColumnLayout.childForceExpandWidth = true;
            urlColumnLayout.childForceExpandHeight = false;
            urlInput = CreateInput(urlColumn.transform, "https://www.youtube.com", 42, false);
            openButton = CreatePrimaryButton(urlCard.transform, "打开", OpenButtonClicked, 82);
            addFavoriteButton = CreateSecondaryButton(urlCard.transform, "加入收藏", AddCurrentUrlToFavorites, 112);
            defaultUrlButton = CreateSecondaryButton(urlCard.transform, "设为默认", SaveCurrentUrlAsDefault, 112);
            cacheButton = CreateSecondaryButton(urlCard.transform, "清除远端缓存", ToggleClearCacheModal, 138);
            settingsButton = CreateSecondaryButton(urlCard.transform, "设置", ToggleStreamSettingsModal, 82);

            var body = CreateUiObject("Remote Body", parent);
            var bodyLayout = body.AddComponent<HorizontalLayoutGroup>();
            bodyLayout.spacing = 32;
            bodyLayout.childControlWidth = true;
            bodyLayout.childControlHeight = true;
            bodyLayout.childForceExpandWidth = false;
            bodyLayout.childForceExpandHeight = true;
            var bodyElement = body.AddComponent<LayoutElement>();
            bodyElement.flexibleHeight = 1;
            bodyElement.minHeight = 590;

            CreateStreamPreview(body.transform);
            BuildRemoteControls(body.transform);

            var overlayLayer = CreateOverlayLayer(parent, "Remote Modal Layer");
            favoritesModal = BuildFavoritesModal(overlayLayer.transform);
            streamSettingsModal = BuildStreamSettingsModal(overlayLayer.transform);
            clearCacheModal = BuildClearCacheModal(overlayLayer.transform);
            streamLogModal = BuildStreamLogModal(overlayLayer.transform);
            streamFullscreenOverlay = BuildStreamFullscreenOverlay(overlayLayer.transform);

            streamModeDropdown = CreateDropdown(overlayLayer.transform);
            streamModeDropdown.gameObject.SetActive(false);
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

            streamResolutionDropdown = CreateDropdown(overlayLayer.transform);
            streamResolutionDropdown.gameObject.SetActive(false);
            streamResolutionDropdown.ClearOptions();
            streamResolutionDropdown.AddOptions(new List<string> { "360p", "540p", "720p", "1080p" });
            streamResolutionDropdown.value = 0;
            streamResolutionDropdown.onValueChanged.AddListener(_ => ScheduleStreamSettingsApply());

            remoteInputPanel = CreateCompactRow(parent, "Remote Input Row", 50);
            remoteInputPanel.GetComponent<LayoutElement>().ignoreLayout = true;
            remoteTextInput = CreateInput(remoteInputPanel.transform, "Page input", 50, false);
            remoteTextInput.onEndEdit.AddListener(_ => RemoteInputEndEdit());
            remoteInputDoneButton = CreateCompactButton(remoteInputPanel.transform, "Done", RemoteInputDoneClicked, 110);
            remoteInputCancelButton = CreateCompactButton(remoteInputPanel.transform, "Cancel", RemoteInputCancelClicked, 130);
            remoteInputPanel.SetActive(false);
        }

        private void CreateBrandHeader(
            Transform parent,
            string pageTitle,
            string subtitle,
            string actionLabel,
            Action action,
            out Button actionButton)
        {
            var row = CreateUiObject(pageTitle + " Header", parent);
            var rowLayout = row.AddComponent<HorizontalLayoutGroup>();
            rowLayout.spacing = 16;
            rowLayout.childControlWidth = true;
            rowLayout.childControlHeight = true;
            rowLayout.childForceExpandWidth = false;
            rowLayout.childForceExpandHeight = false;
            row.AddComponent<LayoutElement>().preferredHeight = 106;

            var logo = CreateLogo(row.transform);
            ApplyLogoLayout(logo);

            var titleColumn = CreateUiObject("Title Column", row.transform);
            titleColumn.AddComponent<LayoutElement>().flexibleWidth = 1;
            var titleLayout = titleColumn.AddComponent<VerticalLayoutGroup>();
            titleLayout.spacing = 0;
            titleLayout.childControlWidth = true;
            titleLayout.childControlHeight = true;
            titleLayout.childForceExpandWidth = true;
            titleLayout.childForceExpandHeight = false;
            CreateText(titleColumn.transform, "RCViewer", 22, FontStyle.Bold, TextAnchor.MiddleLeft, 28).color = Theme.PrimaryText;
            var subtitleText = CreateText(titleColumn.transform, subtitle, 12, FontStyle.Normal, TextAnchor.MiddleLeft, 18);
            subtitleText.color = Theme.MutedText;
            CreateText(titleColumn.transform, pageTitle, 30, FontStyle.Bold, TextAnchor.MiddleLeft, 52).color = Theme.PrimaryText;

            actionButton = CreatePrimaryButton(row.transform, actionLabel, action, 178);
        }

        private void BuildRemoteControls(Transform parent)
        {
            var controls = CreateCard(parent, "TV Remote Controls");
            var controlsElement = controls.AddComponent<LayoutElement>();
            controlsElement.preferredWidth = 448;
            controlsElement.minWidth = 420;
            controlsElement.flexibleWidth = 0;
            controlsElement.minHeight = 470;
            controlsElement.preferredHeight = 470;
            controlsElement.flexibleHeight = 0;
            var layout = controls.AddComponent<VerticalLayoutGroup>();
            layout.padding = new RectOffset(30, 30, 24, 24);
            layout.spacing = 14;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = true;
            layout.childForceExpandHeight = false;

            CreateCardTitle(controls.transform, "电视遥控", 30);
            CreateLabel(controls.transform, "页面导航");
            var navRow = CreateCompactRow(controls.transform, "Navigation Controls", 44);
            backButton = CreateSecondaryButton(navRow.transform, "向后", () => FireAndForget(() => SendSimpleCommandAsync("back")), 112);
            forwardButton = CreateSecondaryButton(navRow.transform, "向前", () => FireAndForget(() => SendSimpleCommandAsync("forward")), 112);
            reloadButton = CreateSecondaryButton(navRow.transform, "刷新", () => FireAndForget(() => SendSimpleCommandAsync("reload")), 88);

            CreateSpacer(controls.transform, 10);
            CreateLabel(controls.transform, "视频控制");
            var mediaRow = CreateCompactRow(controls.transform, "Media Controls", 70);
            var mediaLayout = mediaRow.GetComponent<HorizontalLayoutGroup>();
            mediaLayout.spacing = 10;
            mediaLayout.childAlignment = TextAnchor.MiddleCenter;
            mediaPreviousButton = CreateRoundButton(mediaRow.transform, "上一个", () => FireAndForget(() => SendMediaCommandAsync("previous")), false);
            mediaSeekBackButton = CreateRoundButton(mediaRow.transform, "10s-", () => FireAndForget(() => SendMediaCommandAsync("seek_back", 10f)), false);
            mediaPlayPauseButton = CreateRoundButton(mediaRow.transform, "播放", () => FireAndForget(() => SendMediaCommandAsync("play_pause")), true);
            mediaSeekForwardButton = CreateRoundButton(mediaRow.transform, "10s+", () => FireAndForget(() => SendMediaCommandAsync("seek_forward", 10f)), false);
            mediaNextButton = CreateRoundButton(mediaRow.transform, "下一个", () => FireAndForget(() => SendMediaCommandAsync("next")), false);

            CreateSpacer(controls.transform, 18);
            CreateLabel(controls.transform, "音量与全屏");
            var volumeRow = CreateCompactRow(controls.transform, "Volume Controls", 70);
            var volumeLayout = volumeRow.GetComponent<HorizontalLayoutGroup>();
            volumeLayout.spacing = 10;
            volumeLayout.childAlignment = TextAnchor.MiddleCenter;
            mediaVolumeDownButton = CreateRoundButton(volumeRow.transform, "音-", () => FireAndForget(() => SendMediaCommandAsync("volume_down", 0.1f)), false);
            mediaMuteButton = CreateRoundButton(volumeRow.transform, "静音", () => FireAndForget(() => SendMediaCommandAsync("mute")), false);
            mediaVolumeUpButton = CreateRoundButton(volumeRow.transform, "音+", () => FireAndForget(() => SendMediaCommandAsync("volume_up", 0.1f)), false);
            mediaFullscreenButton = CreateRoundButton(volumeRow.transform, "全屏", () => FireAndForget(() => SendMediaCommandAsync("fullscreen")), false);
            mediaExitFullscreenButton = CreateDangerRoundButton(volumeRow.transform, "退出", () => FireAndForget(() => SendMediaCommandAsync("exit_fullscreen")));

        }

        private GameObject BuildFavoritesModal(Transform parent)
        {
            var modal = CreateModal(parent, "Modal Favorites", new Vector2(400, 360), new Vector2(-330, -20));
            AddModalCloseButton(modal, ToggleFavoritesModal);
            var layout = modal.AddComponent<VerticalLayoutGroup>();
            layout.padding = new RectOffset(28, 28, 22, 24);
            layout.spacing = 14;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = true;
            layout.childForceExpandHeight = false;
            CreateCardTitle(modal.transform, "收藏网站", 32);

            favoriteButtonsContainer = CreateUiObject("Favorite Buttons", modal.transform).transform;
            var grid = favoriteButtonsContainer.gameObject.AddComponent<GridLayoutGroup>();
            grid.cellSize = new Vector2(140, 34);
            grid.spacing = new Vector2(30, 14);
            grid.constraint = GridLayoutGroup.Constraint.FixedColumnCount;
            grid.constraintCount = 2;
            favoriteButtonsContainer.gameObject.AddComponent<LayoutElement>().preferredHeight = 150;

            CreateLabel(modal.transform, "自定义网站");
            var customRow = CreateCompactRow(modal.transform, "Custom Favorite Row", 44);
            customFavoriteInput = CreateInput(customRow.transform, "https://", 42, false);
            addFavoriteButton = CreatePrimaryButton(customRow.transform, "添加", AddCustomFavorite, 72);
            deleteFavoriteButton = CreateSecondaryButton(customRow.transform, "删除", DeleteSelectedFavorite, 72);
            modal.SetActive(false);
            return modal;
        }

        private GameObject BuildStreamSettingsModal(Transform parent)
        {
            var modal = CreateModal(parent, "Modal Stream Settings", new Vector2(330, 330), new Vector2(0, -10));
            AddModalCloseButton(modal, ToggleStreamSettingsModal);
            var layout = modal.AddComponent<VerticalLayoutGroup>();
            layout.padding = new RectOffset(28, 28, 22, 24);
            layout.spacing = 14;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = true;
            layout.childForceExpandHeight = false;
            CreateCardTitle(modal.transform, "画质与帧率", 34);
            CreateLabel(modal.transform, "画质");
            resolutionButtonsContainer = CreateUiObject("Resolution Buttons", modal.transform).transform;
            var resolutionLayout = resolutionButtonsContainer.gameObject.AddComponent<HorizontalLayoutGroup>();
            resolutionLayout.spacing = 10;
            resolutionLayout.childControlWidth = true;
            resolutionLayout.childControlHeight = true;
            resolutionLayout.childForceExpandWidth = false;
            resolutionLayout.childForceExpandHeight = false;
            resolutionButtonsContainer.gameObject.AddComponent<LayoutElement>().preferredHeight = 38;
            foreach (var resolution in new[] { "360p", "540p", "720p", "1080p" })
            {
                var captured = resolution;
                var button = CreatePillButton(resolutionButtonsContainer, captured, () => SelectStreamResolution(captured), 62);
                resolutionButtons.Add(button);
            }

            CreateLabel(modal.transform, "帧率");
            var sliderRow = CreateCompactRow(modal.transform, "FPS Slider Row", 44);
            streamFpsSlider = CreateSlider(
                sliderRow.transform,
                RemoteExplorerClient.MinStreamFps,
                RemoteExplorerClient.MaxStreamFps,
                RemoteExplorerClient.DefaultStreamFps,
                190);
            streamFpsSlider.onValueChanged.AddListener(_ =>
            {
                UpdateStreamFpsLabel();
                settings.StreamFps = SelectedStreamFps();
                ScheduleStreamSettingsApply();
            });
            streamFpsText = CreateText(sliderRow.transform, "30 FPS", 14, FontStyle.Bold, TextAnchor.MiddleCenter, 44);
            var fpsLayout = streamFpsText.GetComponent<LayoutElement>();
            fpsLayout.preferredWidth = 78;
            fpsLayout.flexibleWidth = 0;

            var saveRow = CreateCompactRow(modal.transform, "Stream Settings Buttons", 44);
            CreatePrimaryButton(saveRow.transform, "保存", SaveStreamSettingsAndClose, 120);
            CreateSecondaryButton(saveRow.transform, "关闭", ToggleStreamSettingsModal, 82);
            modal.SetActive(false);
            return modal;
        }

        private GameObject BuildClearCacheModal(Transform parent)
        {
            var modal = CreateModal(parent, "Modal Clear Cache", new Vector2(360, 260), new Vector2(330, -30));
            AddModalCloseButton(modal, ToggleClearCacheModal);
            var layout = modal.AddComponent<VerticalLayoutGroup>();
            layout.padding = new RectOffset(28, 28, 22, 24);
            layout.spacing = 16;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = true;
            layout.childForceExpandHeight = false;
            CreateCardTitle(modal.transform, "清除远端缓存", 32);
            var desc = CreateText(modal.transform, "将操作服务端 Chrome 清理图片/视频缓存。Cookie 登录凭据可选清除。", 13, FontStyle.Normal, TextAnchor.MiddleLeft, 48);
            desc.color = Theme.MutedText;
            clearCookiesToggle = CreateToggle(modal.transform, "清除 Cookie / 登录凭据", 40);
            var buttonRow = CreateCompactRow(modal.transform, "Clear Cache Buttons", 44);
            confirmClearCacheButton = CreatePrimaryButton(buttonRow.transform, "确认清除", ConfirmClearCacheClicked, 120);
            CreateSecondaryButton(buttonRow.transform, "取消", ToggleClearCacheModal, 82);
            modal.SetActive(false);
            return modal;
        }

        private GameObject BuildStreamLogModal(Transform parent)
        {
            var modal = CreateModal(parent, "Modal Stream Log", new Vector2(560, 420), new Vector2(0, -10));
            AddModalCloseButton(modal, ToggleStreamLogModal);
            var layout = modal.AddComponent<VerticalLayoutGroup>();
            layout.padding = new RectOffset(28, 28, 22, 24);
            layout.spacing = 14;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = true;
            layout.childForceExpandHeight = false;
            CreateCardTitle(modal.transform, "串流日志", 34);

            var logPanel = CreateCard(modal.transform, "Stream Log Body", Theme.Background, 14);
            var logPanelElement = logPanel.AddComponent<LayoutElement>();
            logPanelElement.preferredHeight = 260;
            logPanelElement.flexibleHeight = 1;
            streamLogText = CreateText(logPanel.transform, "暂无串流日志", 12, FontStyle.Normal, TextAnchor.UpperLeft, 0);
            streamLogText.color = Theme.MutedText;
            streamLogText.verticalOverflow = VerticalWrapMode.Truncate;
            Stretch(streamLogText.rectTransform, 14, 14, 12, 12);

            var buttonRow = CreateCompactRow(modal.transform, "Stream Log Buttons", 44);
            var rowLayout = buttonRow.GetComponent<HorizontalLayoutGroup>();
            rowLayout.childAlignment = TextAnchor.MiddleRight;
            var spacer = CreateUiObject("Stream Log Spacer", buttonRow.transform);
            spacer.AddComponent<LayoutElement>().flexibleWidth = 1;
            CreateSecondaryButton(buttonRow.transform, "清空", ClearStreamLog, 82);
            CreatePrimaryButton(buttonRow.transform, "关闭", ToggleStreamLogModal, 82);
            modal.SetActive(false);
            return modal;
        }

        private GameObject BuildStreamFullscreenOverlay(Transform parent)
        {
            var overlay = CreatePanel(parent, "Stream Fullscreen Overlay", new Color(0f, 0f, 0f, 0.96f));
            overlay.AddComponent<LayoutElement>().ignoreLayout = true;
            Stretch(overlay.GetComponent<RectTransform>(), 0, 0, 0, 0);

            var layout = overlay.AddComponent<VerticalLayoutGroup>();
            layout.padding = new RectOffset(24, 24, 24, 24);
            layout.spacing = 12;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = true;
            layout.childForceExpandHeight = false;

            streamFullscreenHost = CreatePanel(overlay.transform, "Fullscreen Stream Host", new Color(0f, 0f, 0f, 0f));
            var hostImage = streamFullscreenHost.GetComponent<Image>();
            if (hostImage != null)
            {
                hostImage.raycastTarget = false;
            }

            var hostElement = streamFullscreenHost.AddComponent<LayoutElement>();
            hostElement.flexibleHeight = 1;
            hostElement.minHeight = 240;
            hostElement.preferredHeight = 900;

            var actionRow = CreateCompactRow(overlay.transform, "Fullscreen Action Row", 44);
            var actionLayout = actionRow.GetComponent<HorizontalLayoutGroup>();
            actionLayout.spacing = 12;
            actionLayout.childAlignment = TextAnchor.MiddleRight;
            var spacer = CreateUiObject("Fullscreen Action Spacer", actionRow.transform);
            spacer.AddComponent<LayoutElement>().flexibleWidth = 1;
            CreateSecondaryButton(actionRow.transform, "退出全屏", ToggleStreamPreviewFullscreen, 120);
            overlay.SetActive(false);
            return overlay;
        }

        private GameObject CreateOverlayLayer(Transform parent, string name)
        {
            var overlay = CreateUiObject(name, parent);
            overlay.AddComponent<LayoutElement>().ignoreLayout = true;
            Stretch(overlay.GetComponent<RectTransform>(), 0, 0, 0, 0);
            return overlay;
        }

        private GameObject CreateModal(Transform parent, string name, Vector2 size, Vector2 position)
        {
            var modal = CreateCard(parent, name, Theme.Modal, 24);
            var rect = modal.GetComponent<RectTransform>();
            Anchor(rect, new Vector2(0.5f, 0.5f), new Vector2(0.5f, 0.5f), position, size);
            return modal;
        }

        private static void AddModalCloseButton(GameObject modal, Action closeAction)
        {
            var close = CreateSecondaryButton(modal.transform, "X", closeAction, 42);
            var element = close.GetComponent<LayoutElement>();
            element.ignoreLayout = true;
            element.preferredWidth = 42;
            element.minWidth = 42;
            element.preferredHeight = 42;
            element.minHeight = 42;
            Anchor(close.GetComponent<RectTransform>(), new Vector2(1, 1), new Vector2(1, 1), new Vector2(-24, -24), new Vector2(42, 42));
        }

        private async Task RefreshServersAsync()
        {
            SetStatus("正在搜索局域网服务器...");
            try
            {
                servers.Clear();
                var discovered = await client.DiscoverAsync(cancellationToken: lifetime.Token);
                servers.AddRange(discovered);
                RebuildServerDropdown();
                SetStatus(servers.Count == 0 ? "未发现在线服务器" : $"发现 {servers.Count} 个在线服务器");
            }
            catch (Exception ex)
            {
                SetStatus("搜索失败: " + ex.Message);
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

                var savedById = settings.Servers.FirstOrDefault(server => server.Id == settings.ServerId);
                if (savedById != null)
                {
                    return SavedToDiscovered(savedById);
                }
            }

            var defaultServer = settings.Servers.FirstOrDefault(server => server.IsDefault) ??
                settings.Servers.FirstOrDefault();
            if (defaultServer != null)
            {
                return SavedToDiscovered(defaultServer);
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
            SetStatus("正在连接 " + server);
            try
            {
                var password = PasswordForServer(server);
                await client.ConnectAsync(server, password, lifetime.Token);
                settings.Password = password;
                settings.AutoConnect = defaultServerToggle != null && defaultServerToggle.isOn;
                settings.ServerId = server.Id;
                settings.ServerHost = server.Address;
                settings.ServerPort = server.ControlPort;
                UpsertSavedServerFromConnection(server, password);
                settings.Save();
                SetCommandButtons(true);
                SetStatus("已连接: " + server);
                ShowRemotePage();
                FireAndForget(StartStreamAsync);
            }
            catch (Exception ex)
            {
                SetCommandButtons(false);
                SetStatus("连接失败: " + ex.Message);
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
                SetStatus("请先输入站点地址");
                return;
            }

            settings.DefaultUrl = urlInput.text.Trim();
            settings.Save();
            SetStatus("正在打开 " + urlInput.text);
            await RunCommandAsync(() => client.NavigateAsync(urlInput.text, lifetime.Token), "已打开站点");
        }

        private async Task ClosePageAsync()
        {
            SetStatus("正在关闭当前页面");
            await RunCommandAsync(() => client.ClosePageAsync(lifetime.Token), "已关闭页面");
        }

        private async Task ClearRemoteCacheAsync(bool clearCookies)
        {
            SetStatus(clearCookies ? "正在清除远端缓存与登录凭据" : "正在清除远端缓存");
            await RunCommandAsync(
                () => client.ClearRemoteCacheAsync(clearCookies, lifetime.Token),
                clearCookies ? "已清除远端缓存与 Cookie" : "已清除远端缓存");
        }

        private void SaveManagedServer()
        {
            string host;
            int port;
            if (!TryParseServerAddress(serverAddressInput != null ? serverAddressInput.text : string.Empty, out host, out port))
            {
                SetStatus("请输入有效服务器地址，例如 192.168.1.20:45454");
                return;
            }

            var name = serverNameInput != null ? serverNameInput.text.Trim() : string.Empty;
            if (string.IsNullOrEmpty(name))
            {
                name = host;
            }

            var id = selectedSavedServerIndex >= 0 && selectedSavedServerIndex < settings.Servers.Count
                ? settings.Servers[selectedSavedServerIndex].Id
                : MakeManualServerId(host, port);
            var saved = new RemoteExplorerSavedServer
            {
                Id = id,
                Name = name,
                Host = host,
                Port = port,
                SavePassword = savePasswordToggle != null && savePasswordToggle.isOn,
                Password = savePasswordToggle != null && savePasswordToggle.isOn && passwordInput != null
                    ? passwordInput.text
                    : string.Empty,
                IsDefault = defaultServerToggle != null && defaultServerToggle.isOn
            };

            if (saved.IsDefault)
            {
                foreach (var item in settings.Servers)
                {
                    item.IsDefault = false;
                }
                settings.AutoConnect = true;
                settings.ServerId = saved.Id;
                settings.ServerHost = saved.Host;
                settings.ServerPort = saved.Port;
                settings.Password = saved.Password;
            }

            var existing = settings.Servers.FindIndex(server => server.Id == saved.Id);
            if (existing >= 0)
            {
                settings.Servers[existing] = saved;
                selectedSavedServerIndex = existing;
            }
            else
            {
                settings.Servers.Add(saved);
                selectedSavedServerIndex = settings.Servers.Count - 1;
            }

            settings.Save();
            RebuildServerDropdown();
            SetStatus("服务器设定已保存");
        }

        private void DeleteManagedServer()
        {
            if (selectedSavedServerIndex < 0 || selectedSavedServerIndex >= settings.Servers.Count)
            {
                SetStatus("请选择一个已保存服务器");
                return;
            }

            var removed = settings.Servers[selectedSavedServerIndex];
            settings.Servers.RemoveAt(selectedSavedServerIndex);
            if (settings.ServerId == removed.Id)
            {
                settings.ServerId = string.Empty;
                settings.ServerHost = string.Empty;
                settings.ServerPort = RemoteExplorerProtocol.DefaultControlPort;
                settings.AutoConnect = false;
            }

            selectedSavedServerIndex = Mathf.Clamp(selectedSavedServerIndex, -1, settings.Servers.Count - 1);
            settings.Save();
            RebuildServerDropdown();
            SetStatus("服务器已删除");
        }

        private void SelectSavedServer(int index)
        {
            selectedSavedServerIndex = index;
            if (index < 0 || index >= settings.Servers.Count)
            {
                return;
            }

            var server = settings.Servers[index];
            if (serverNameInput != null)
            {
                serverNameInput.text = server.Name ?? string.Empty;
            }

            if (serverAddressInput != null)
            {
                serverAddressInput.text = $"{server.Host}:{server.Port}";
            }

            if (passwordInput != null)
            {
                passwordInput.text = server.SavePassword ? server.Password ?? string.Empty : string.Empty;
            }

            if (savePasswordToggle != null)
            {
                savePasswordToggle.isOn = server.SavePassword;
            }

            if (defaultServerToggle != null)
            {
                defaultServerToggle.isOn = server.IsDefault;
            }
        }

        private void ConnectSavedServer(int index)
        {
            SelectSavedServer(index);
            if (index >= 0 && index < settings.Servers.Count)
            {
                FireAndForget(() => ConnectAsync(SavedToDiscovered(settings.Servers[index])));
            }
        }

        private void ConnectDiscoveredServer(DiscoveredServer server)
        {
            if (serverNameInput != null)
            {
                serverNameInput.text = string.IsNullOrEmpty(server.Name) ? server.Address : server.Name;
            }

            if (serverAddressInput != null)
            {
                serverAddressInput.text = $"{server.Address}:{server.ControlPort}";
            }

            selectedSavedServerIndex = settings.Servers.FindIndex(saved =>
                saved.Id == server.Id ||
                (string.Equals(saved.Host, server.Address, StringComparison.OrdinalIgnoreCase) && saved.Port == server.ControlPort));
            FireAndForget(() => ConnectAsync(server));
        }

        private void AddCurrentUrlToFavorites()
        {
            if (urlInput == null || string.IsNullOrWhiteSpace(urlInput.text))
            {
                SetStatus("请先输入站点地址");
                return;
            }

            AddFavorite(urlInput.text.Trim());
        }

        private void AddCustomFavorite()
        {
            if (customFavoriteInput == null || string.IsNullOrWhiteSpace(customFavoriteInput.text))
            {
                SetStatus("请输入自定义网站地址");
                return;
            }

            AddFavorite(customFavoriteInput.text.Trim());
            customFavoriteInput.text = string.Empty;
        }

        private void AddFavorite(string url)
        {
            var normalized = NormalizeDisplayUrl(url);
            if (settings.Favorites.Any(favorite => string.Equals(favorite.Url, normalized, StringComparison.OrdinalIgnoreCase)))
            {
                SetStatus("收藏已存在");
                return;
            }

            settings.Favorites.Add(new RemoteExplorerFavoriteSite
            {
                Name = FavoriteNameFromUrl(normalized),
                Url = normalized
            });
            settings.Save();
            BuildFavoriteButtons();
            SetStatus("已加入收藏");
        }

        private void DeleteSelectedFavorite()
        {
            var target = !string.IsNullOrEmpty(selectedFavoriteUrl)
                ? selectedFavoriteUrl
                : customFavoriteInput != null ? NormalizeDisplayUrl(customFavoriteInput.text) : string.Empty;
            var index = settings.Favorites.FindIndex(favorite =>
                string.Equals(favorite.Url, target, StringComparison.OrdinalIgnoreCase));
            if (index < 0)
            {
                SetStatus("请选择或输入要删除的收藏");
                return;
            }

            settings.Favorites.RemoveAt(index);
            if (settings.Favorites.Count == 0)
            {
                settings.Favorites.AddRange(RemoteExplorerSettings.DefaultFavorites());
            }

            selectedFavoriteUrl = string.Empty;
            settings.Save();
            BuildFavoriteButtons();
            SetStatus("收藏已删除");
        }

        private void OpenFavorite(RemoteExplorerFavoriteSite favorite)
        {
            if (urlInput != null)
            {
                urlInput.text = favorite.Url;
            }

            selectedFavoriteUrl = favorite.Url;
            ToggleFavoritesModal();
            FireAndForget(OpenUrlAsync);
        }

        private void SaveCurrentUrlAsDefault()
        {
            if (urlInput == null || string.IsNullOrWhiteSpace(urlInput.text))
            {
                SetStatus("请先输入站点地址");
                return;
            }

            settings.DefaultUrl = NormalizeDisplayUrl(urlInput.text);
            settings.Save();
            SetStatus("默认站点已保存");
        }

        private void ToggleFavoritesModal()
        {
            if (favoritesModal == null)
            {
                return;
            }

            BuildFavoriteButtons();
            favoritesModal.SetActive(!favoritesModal.activeSelf);
            HideModal(streamSettingsModal);
            HideModal(clearCacheModal);
            HideModal(streamLogModal);
        }

        private void ToggleStreamSettingsModal()
        {
            if (streamSettingsModal == null)
            {
                return;
            }

            UpdateResolutionButtons();
            streamSettingsModal.SetActive(!streamSettingsModal.activeSelf);
            HideModal(favoritesModal);
            HideModal(clearCacheModal);
            HideModal(streamLogModal);
        }

        private void ToggleClearCacheModal()
        {
            if (clearCacheModal == null)
            {
                return;
            }

            clearCacheModal.SetActive(!clearCacheModal.activeSelf);
            HideModal(favoritesModal);
            HideModal(streamSettingsModal);
            HideModal(streamLogModal);
        }

        private void ToggleStreamLogModal()
        {
            if (streamLogModal == null)
            {
                return;
            }

            UpdateStreamLogText();
            streamLogModal.SetActive(!streamLogModal.activeSelf);
            HideModal(favoritesModal);
            HideModal(streamSettingsModal);
            HideModal(clearCacheModal);
        }

        private void ClearStreamLog()
        {
            streamStatusLog.Clear();
            UpdateStreamLogText();
        }

        private void ConfirmClearCacheClicked()
        {
            var clearCookies = clearCookiesToggle != null && clearCookiesToggle.isOn;
            ToggleClearCacheModal();
            FireAndForget(() => ClearRemoteCacheAsync(clearCookies));
        }

        private void SelectStreamResolution(string resolution)
        {
            settings.StreamResolution = resolution;
            if (streamResolutionDropdown != null)
            {
                var index = streamResolutionDropdown.options.FindIndex(option => option.text == resolution);
                if (index >= 0)
                {
                    streamResolutionDropdown.value = index;
                }
            }

            UpdateResolutionButtons();
            ScheduleStreamSettingsApply();
        }

        private void SaveStreamSettingsAndClose()
        {
            settings.StreamResolution = SelectedStreamResolution();
            settings.StreamFps = SelectedStreamFps();
            settings.StreamMode = StreamModePreference(SelectedStreamMode());
            settings.Save();
            HideModal(streamSettingsModal);
            SetStatus("画质与帧率已保存");
        }

        private void ToggleStreamPreviewFullscreen()
        {
            if (streamImage == null || streamFullscreenOverlay == null || streamFullscreenHost == null)
            {
                return;
            }

            var imageRect = streamImage.rectTransform;
            if (!streamPreviewFullscreen)
            {
                streamImageOriginalParent = imageRect.parent;
                streamImageOriginalSiblingIndex = imageRect.GetSiblingIndex();
                imageRect.SetParent(streamFullscreenHost.transform, false);
                Stretch(imageRect, 0, 0, 0, 0);
                streamFullscreenOverlay.SetActive(true);
                streamPreviewFullscreen = true;
                SetButtonLabel(streamFullscreenButton, "关闭全屏");
                return;
            }

            imageRect.SetParent(streamImageOriginalParent, false);
            imageRect.SetSiblingIndex(streamImageOriginalSiblingIndex);
            Stretch(imageRect, 0, 0, 0, 0);
            streamFullscreenOverlay.SetActive(false);
            streamPreviewFullscreen = false;
            SetButtonLabel(streamFullscreenButton, "全屏展示");
        }

        private async void ExitRemoteSessionClicked()
        {
            try
            {
                await StopStreamAsync();
            }
            finally
            {
                client.Disconnect();
                SetCommandButtons(false);
                ShowConnectPage();
                SetStatus("已断开服务器连接");
            }
        }

        private void HandleConnectionLost(DiscoveredServer server, string reason)
        {
            if (lifetime == null || lifetime.IsCancellationRequested)
            {
                return;
            }

            MarkServerOffline(server);
            streamRestartInFlight = false;
            streamStartInFlight = false;
            streamSettingsDirty = false;
            connectionHealthCheckInFlight = false;
            nextConnectionHealthCheckAt = -1f;
            hasActiveStreamMode = false;
            lastStreamFrameAt = -1f;
            nextStreamWatchdogAt = -1f;
            remoteKeyboardCommands.Clear();
#if REMOTE_EXPLORER_HAS_WEBRTC
            if (webRtcPlayback != null && webRtcPlayback.IsActive)
            {
                FireAndForget(() => webRtcPlayback.StopAsync(lifetime.Token));
            }
#endif
            ResetStreamRenderStats();
            SetButtonLabel(streamToggleButton, "打开串流");
            SetStreamStatus("服务器连接已断开");
            SetCommandButtons(false);
            ShowConnectPage();
            SetStatus("服务器连接已断开: " + reason);
        }

        private void MarkServerOffline(DiscoveredServer offlineServer)
        {
            if (offlineServer == null)
            {
                return;
            }

            var removed = servers.RemoveAll(server => SameServer(server, offlineServer));
            if (removed > 0)
            {
                RebuildServerDropdown();
            }
        }

        private static bool SameServer(DiscoveredServer left, DiscoveredServer right)
        {
            if (left == null || right == null)
            {
                return false;
            }

            if (!string.IsNullOrEmpty(left.Id) && !string.IsNullOrEmpty(right.Id) && left.Id == right.Id)
            {
                return true;
            }

            return string.Equals(left.Address, right.Address, StringComparison.OrdinalIgnoreCase) &&
                left.ControlPort == right.ControlPort;
        }

        private void UpdateConnectionHealth()
        {
            if (!client.IsConnected || connectionHealthCheckInFlight || Time.unscaledTime < nextConnectionHealthCheckAt)
            {
                return;
            }

            connectionHealthCheckInFlight = true;
            nextConnectionHealthCheckAt = Time.unscaledTime + ConnectionHealthIntervalSeconds;
            FireAndForget(CheckConnectionHealthAsync);
        }

        private async Task CheckConnectionHealthAsync()
        {
            try
            {
                await client.BrowserCommandAsync("status", lifetime.Token, ConnectionHealthTimeoutSeconds);
            }
            catch (Exception ex)
            {
                if (client.IsConnected)
                {
                    Debug.LogWarning("[RemoteExplorer] Connection health check failed: " + ex.Message);
                }
            }
            finally
            {
                connectionHealthCheckInFlight = false;
            }
        }

        private void HideModal(GameObject modal)
        {
            if (modal != null)
            {
                modal.SetActive(false);
            }
        }

        private void BuildFavoriteButtons()
        {
            if (favoriteButtonsContainer == null)
            {
                return;
            }

            ClearChildren(favoriteButtonsContainer);
            foreach (var favorite in settings.Favorites)
            {
                var captured = favorite;
                var button = CreatePillButton(favoriteButtonsContainer, favorite.Name, () => OpenFavorite(captured), 140);
                var text = button.GetComponentInChildren<Text>();
                if (text != null)
                {
                    text.alignment = TextAnchor.MiddleLeft;
                    Stretch(text.rectTransform, 16, 12, 0, 0);
                }
            }
        }

        private void UpdateResolutionButtons()
        {
            var selected = SelectedStreamResolution();
            for (var i = 0; i < resolutionButtons.Count; i++)
            {
                var button = resolutionButtons[i];
                if (button == null)
                {
                    continue;
                }

                var image = button.GetComponent<Image>();
                var label = button.GetComponentInChildren<Text>();
                var isSelected = label != null && label.text == selected;
                if (image != null)
                {
                    image.color = isSelected ? Theme.PrimaryButton : Theme.Pill;
                }
            }
        }

        private RemoteExplorerSavedServer FindSavedServerRecord(DiscoveredServer server)
        {
            return settings.Servers.FirstOrDefault(saved =>
                saved.Id == server.Id ||
                (string.Equals(saved.Host, server.Address, StringComparison.OrdinalIgnoreCase) && saved.Port == server.ControlPort));
        }

        private string PasswordForServer(DiscoveredServer server)
        {
            var saved = FindSavedServerRecord(server);
            if (saved != null && saved.SavePassword)
            {
                return saved.Password ?? string.Empty;
            }

            return passwordInput != null ? passwordInput.text : string.Empty;
        }

        private void UpsertSavedServerFromConnection(DiscoveredServer server, string password)
        {
            var savePassword = savePasswordToggle != null && savePasswordToggle.isOn;
            var isDefault = defaultServerToggle != null && defaultServerToggle.isOn;
            var name = serverNameInput != null && !string.IsNullOrWhiteSpace(serverNameInput.text)
                ? serverNameInput.text.Trim()
                : string.IsNullOrEmpty(server.Name) ? server.Address : server.Name;
            var id = string.IsNullOrEmpty(server.Id) ? MakeManualServerId(server.Address, server.ControlPort) : server.Id;
            var existing = settings.Servers.FindIndex(saved =>
                saved.Id == id ||
                (string.Equals(saved.Host, server.Address, StringComparison.OrdinalIgnoreCase) && saved.Port == server.ControlPort));

            if (isDefault)
            {
                foreach (var item in settings.Servers)
                {
                    item.IsDefault = false;
                }
            }

            var savedServer = new RemoteExplorerSavedServer
            {
                Id = id,
                Name = name,
                Host = server.Address,
                Port = server.ControlPort,
                Password = savePassword ? password : string.Empty,
                SavePassword = savePassword,
                IsDefault = isDefault
            };

            if (existing >= 0)
            {
                settings.Servers[existing] = savedServer;
                selectedSavedServerIndex = existing;
            }
            else
            {
                settings.Servers.Add(savedServer);
                selectedSavedServerIndex = settings.Servers.Count - 1;
            }
        }

        private static DiscoveredServer SavedToDiscovered(RemoteExplorerSavedServer saved)
        {
            return new DiscoveredServer
            {
                Address = saved.Host,
                ControlPort = saved.Port > 0 ? saved.Port : RemoteExplorerProtocol.DefaultControlPort,
                Id = saved.Id,
                Name = saved.Name,
                AuthMode = string.IsNullOrEmpty(saved.Password) ? "none" : "password",
                Capabilities = new string[0]
            };
        }

        private static bool TryParseServerAddress(string value, out string host, out int port)
        {
            host = string.Empty;
            port = RemoteExplorerProtocol.DefaultControlPort;
            if (string.IsNullOrWhiteSpace(value))
            {
                return false;
            }

            var trimmed = value.Trim();
            if (trimmed.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
                trimmed.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            {
                Uri uri;
                if (!Uri.TryCreate(trimmed, UriKind.Absolute, out uri))
                {
                    return false;
                }

                host = uri.Host;
                port = uri.Port > 0 ? uri.Port : RemoteExplorerProtocol.DefaultControlPort;
                return !string.IsNullOrEmpty(host);
            }

            var lastColon = trimmed.LastIndexOf(':');
            if (lastColon > 0 && lastColon < trimmed.Length - 1)
            {
                host = trimmed.Substring(0, lastColon).Trim();
                int parsedPort;
                if (!int.TryParse(trimmed.Substring(lastColon + 1).Trim(), out parsedPort))
                {
                    return false;
                }

                port = parsedPort;
            }
            else
            {
                host = trimmed;
            }

            return !string.IsNullOrEmpty(host) && port > 0 && port <= 65535;
        }

        private static string MakeManualServerId(string host, int port)
        {
            return "manual:" + (host ?? string.Empty).Trim().ToLowerInvariant() + ":" + port;
        }

        private static string NormalizeDisplayUrl(string url)
        {
            if (string.IsNullOrWhiteSpace(url))
            {
                return "https://";
            }

            var trimmed = url.Trim();
            if (trimmed.StartsWith("about:", StringComparison.OrdinalIgnoreCase) ||
                trimmed.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
                trimmed.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            {
                return trimmed;
            }

            return "https://" + trimmed;
        }

        private static string FavoriteNameFromUrl(string url)
        {
            Uri uri;
            if (!Uri.TryCreate(NormalizeDisplayUrl(url), UriKind.Absolute, out uri) || string.IsNullOrEmpty(uri.Host))
            {
                return url;
            }

            var host = uri.Host;
            if (host.StartsWith("www.", StringComparison.OrdinalIgnoreCase))
            {
                host = host.Substring(4);
            }

            var first = host.Split('.')[0];
            return string.IsNullOrEmpty(first) ? host : char.ToUpperInvariant(first[0]) + first.Substring(1);
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
                if (!client.IsConnected)
                {
                    return;
                }

                if (result.ok || result.type == "result")
                {
                    UpdateMediaControlLabels(result.result);
                    SetStatus(DescribeMediaStatus(result.result));
                    return;
                }

                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                SetStatus("Player status failed: " + error);
            }
            catch (Exception ex)
            {
                if (!client.IsConnected)
                {
                    return;
                }

                SetStatus("Player status failed: " + ex.Message);
            }
        }

        private async Task SendMediaCommandAsync(string action, float amount = 0f)
        {
            SetStatus("Media " + MediaActionLabel(action));
            try
            {
                var result = await client.MediaControlAsync(action, amount, lifetime.Token);
                if (!client.IsConnected)
                {
                    return;
                }

                if (result.ok || result.type == "result")
                {
                    UpdateMediaControlLabels(result.result);
                    SetStatus(DescribeMediaCommand(result.result, action));
                    return;
                }

                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                SetStatus("Media command failed: " + error);
            }
            catch (Exception ex)
            {
                if (!client.IsConnected)
                {
                    return;
                }

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

        private void UpdateMediaControlLabels(WebRtcAnswer media)
        {
            if (media == null || !media.media_found)
            {
                return;
            }

            SetButtonLabel(mediaPlayPauseButton, media.media_paused ? "播放" : "暂停");
            SetButtonLabel(mediaMuteButton, media.media_muted ? "解除" : "静音");
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
                    return "播放/暂停";
                case "fullscreen":
                    return "全屏";
                case "exit_fullscreen":
                    return "Exit fullscreen";
                case "volume_up":
                    return "音量加";
                case "volume_down":
                    return "音量减";
                case "mute":
                    return "静音";
                case "seek_forward":
                    return "快进";
                case "seek_back":
                    return "快退";
                case "next":
                    return "下一个";
                case "previous":
                    return "上一个";
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
                SetButtonLabel(streamToggleButton, "打开串流");
                return;
            }

            SetStatus($"Starting WebRTC {resolution} at {fps} fps");
            try
            {
                var webRtcResult = await webRtcPlayback.StartAsync(resolution, fps, lifetime.Token);
                if (!client.IsConnected)
                {
                    return;
                }

                if (webRtcResult.ok || webRtcResult.type == "result")
                {
                    SetButtonLabel(streamToggleButton, "关闭串流");
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
                SetButtonLabel(streamToggleButton, "打开串流");
            }
            catch (Exception ex)
            {
                if (!client.IsConnected)
                {
                    return;
                }

                SetStreamStatus("WebRTC failed: " + ex.Message);
                SetStatus("WebRTC stream failed: " + ex.Message);
                SetButtonLabel(streamToggleButton, "打开串流");
            }
#else
            SetStreamStatus("WebRTC is not available in this client build");
            SetStatus("WebRTC is not available in this client build");
            SetButtonLabel(streamToggleButton, "打开串流");
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
                if (!client.IsConnected)
                {
                    return;
                }

                if (result.ok || result.type == "result")
                {
                    SetButtonLabel(streamToggleButton, "关闭串流");
                    activeStreamMode = RemoteStreamMode.UdpJpeg;
                    hasActiveStreamMode = true;
                    ResetStreamRenderStats();
                    MarkStreamFrameClock();
                    SetStreamStatus($"Waiting for {resolution} / {fps}fps frames...");
                    SetStatus("串流已开启");
                    return;
                }

                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                SetStatus("Stream failed: " + error);
            }
            catch (Exception ex)
            {
                if (!client.IsConnected)
                {
                    return;
                }

                SetStatus("Stream failed: " + ex.Message);
            }
        }

        private async Task StopStreamAsync()
        {
            await StopAllStreamTransportsAsync();
            SetButtonLabel(streamToggleButton, "打开串流");
            SetStreamStatus("串流已关闭");
            SetStatus("串流已关闭");
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
                if (!client.IsConnected)
                {
                    return;
                }

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
                if (!client.IsConnected)
                {
                    return;
                }

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
                if (!client.IsConnected)
                {
                    return;
                }

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
                if (!client.IsConnected)
                {
                    return;
                }

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
                if (!client.IsConnected)
                {
                    return;
                }

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
                if (!client.IsConnected)
                {
                    return;
                }

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
                if (!client.IsConnected)
                {
                    return;
                }

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
                    if (!client.IsConnected)
                    {
                        remoteKeyboardCommands.Clear();
                        break;
                    }

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
                if (!client.IsConnected)
                {
                    return;
                }

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
                if (!client.IsConnected)
                {
                    return;
                }

                SetStatus("Command failed: " + ex.Message);
            }
        }

        private void ApplySettingsToUi()
        {
            fallbackUrl = string.IsNullOrEmpty(settings.DefaultUrl) ? "https://www.youtube.com" : settings.DefaultUrl;
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

            if (streamResolutionDropdown != null && streamResolutionDropdown.options.Count > 0)
            {
                var resolutionIndex = streamResolutionDropdown.options.FindIndex(option => option.text == settings.StreamResolution);
                streamResolutionDropdown.value = resolutionIndex >= 0 ? resolutionIndex : 2;
            }

            if (streamFpsSlider != null)
            {
                streamFpsSlider.value = Mathf.Clamp(
                    settings.StreamFps,
                    RemoteExplorerClient.MinStreamFps,
                    RemoteExplorerClient.MaxStreamFps);
                UpdateStreamFpsLabel();
            }

            if (streamModeDropdown != null)
            {
                streamModeDropdown.value = PreferredStreamModeIndex(settings.StreamMode);
            }

            var selectedIndex = settings.Servers.FindIndex(server => server.IsDefault);
            if (selectedIndex < 0 && settings.Servers.Count > 0)
            {
                selectedIndex = 0;
            }

            if (selectedIndex >= 0)
            {
                SelectSavedServer(selectedIndex);
            }

            RebuildServerDropdown();
            BuildFavoriteButtons();
            UpdateResolutionButtons();
        }

        private void SaveSettings()
        {
            settings.Password = passwordInput != null ? passwordInput.text : string.Empty;
            settings.AutoConnect = defaultServerToggle != null && defaultServerToggle.isOn;
            settings.StreamMode = StreamModePreference(SelectedStreamMode());
            settings.StreamResolution = SelectedStreamResolution();
            settings.StreamFps = SelectedStreamFps();
            if (SelectedServer() != null)
            {
                var server = SelectedServer();
                settings.ServerId = server.Id;
                settings.ServerHost = server.Address;
                settings.ServerPort = server.ControlPort;
            }

            settings.Save();
            SetStatus("设定已保存");
        }

private void RebuildServerDropdown()
        {
            if (serverDropdown != null)
            {
                serverDropdown.ClearOptions();
                var options = settings.Servers.Select(server => $"{server.Name} ({server.Host}:{server.Port})").ToList();
                options.AddRange(servers.Select(server => server.ToString()));
                serverDropdown.AddOptions(options.Count == 0 ? new List<string> { "No servers found" } : options);
            }

            if (serverRowsContainer == null)
            {
                return;
            }

            ClearChildren(serverRowsContainer.transform);
            serverRowButtons.Clear();
            for (var i = 0; i < settings.Servers.Count; i++)
            {
                var capturedIndex = i;
                var saved = settings.Servers[i];
                var isOnline = servers.Any(server =>
                    server.Id == saved.Id ||
                    (string.Equals(server.Address, saved.Host, StringComparison.OrdinalIgnoreCase) && server.ControlPort == saved.Port));
                var displayName = string.IsNullOrEmpty(saved.Name) ? saved.Host : saved.Name;
                var address = $"{saved.Host}:{saved.Port}";
                var connectButton = CreateServerRow(
                    serverRowsContainer.transform,
                    displayName,
                    address,
                    isOnline,
                    saved.IsDefault,
                    () =>
                    {
                        SelectSavedServer(capturedIndex);
                        SetStatus("已加载到左侧编辑表单");
                    },
                    () =>
                    {
                        if (!isOnline)
                        {
                            SetStatus("该服务器离线");
                            return;
                        }

                        ConnectSavedServer(capturedIndex);
                    });
                serverRowButtons.Add(connectButton);
            }

            foreach (var discovered in servers)
            {
                var exists = settings.Servers.Any(saved =>
                    saved.Id == discovered.Id ||
                    (string.Equals(saved.Host, discovered.Address, StringComparison.OrdinalIgnoreCase) && saved.Port == discovered.ControlPort));
                if (exists)
                {
                    continue;
                }

                var captured = discovered;
                var discoveredName = string.IsNullOrEmpty(discovered.Name) ? discovered.Address : discovered.Name;
                CreateServerRow(
                    serverRowsContainer.transform,
                    discoveredName,
                    $"{discovered.Address}:{discovered.ControlPort}",
                    true,
                    false,
                    () =>
                    {
                        selectedSavedServerIndex = -1;
                        if (serverNameInput != null)
                        {
                            serverNameInput.text = discoveredName;
                        }

                        if (serverAddressInput != null)
                        {
                            serverAddressInput.text = $"{discovered.Address}:{discovered.ControlPort}";
                        }

                        SetStatus("已加载到左侧编辑表单");
                    },
                    () => ConnectDiscoveredServer(captured));
            }

            if (settings.Servers.Count == 0 && servers.Count == 0)
            {
                var empty = CreateText(serverRowsContainer.transform, "暂无服务器，点击右上角搜索或手动添加。", 15, FontStyle.Normal, TextAnchor.MiddleLeft, 72);
                empty.color = Theme.MutedText;
            }
        }

        private DiscoveredServer SelectedServer()
        {
            if (selectedSavedServerIndex >= 0 && selectedSavedServerIndex < settings.Servers.Count)
            {
                return SavedToDiscovered(settings.Servers[selectedSavedServerIndex]);
            }

            if (servers.Count == 0 || serverDropdown == null || serverDropdown.value < settings.Servers.Count ||
                serverDropdown.value >= settings.Servers.Count + servers.Count)
            {
                return null;
            }

            return servers[serverDropdown.value - settings.Servers.Count];
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
                SetStatus("请先选择或添加服务器");
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

            if (remoteSubtitleText != null && client.ConnectedServer != null)
            {
                var server = client.ConnectedServer;
                remoteSubtitleText.text = $"已连接：{(string.IsNullOrEmpty(server.Name) ? server.Address : server.Name)} / {server.Address}:{server.ControlPort}";
            }

            if (urlInput != null && string.IsNullOrWhiteSpace(urlInput.text))
            {
                urlInput.text = settings.DefaultUrl;
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

            RebuildServerDropdown();
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
            SetButtonInteractable(favoritesButton, enabled);
            SetButtonInteractable(addFavoriteButton, enabled);
            SetButtonInteractable(defaultUrlButton, enabled);
            SetButtonInteractable(cacheButton, enabled);
            SetButtonInteractable(settingsButton, true);
            SetButtonInteractable(exitButton, enabled);
            SetButtonInteractable(streamFullscreenButton, enabled);
            SetButtonInteractable(streamLogButton, true);
            SetButtonInteractable(confirmClearCacheButton, enabled);
            SetButtonInteractable(backButton, enabled);
            SetButtonInteractable(forwardButton, enabled);
            SetButtonInteractable(reloadButton, enabled);
            SetButtonInteractable(streamToggleButton, enabled);
            SetButtonInteractable(remoteInputDoneButton, enabled);
            SetButtonInteractable(remoteInputCancelButton, enabled);
            SetButtonInteractable(mediaPreviousButton, enabled);
            SetButtonInteractable(mediaSeekBackButton, enabled);
            SetButtonInteractable(mediaPlayPauseButton, enabled);
            SetButtonInteractable(mediaSeekForwardButton, enabled);
            SetButtonInteractable(mediaNextButton, enabled);
            SetButtonInteractable(mediaFullscreenButton, enabled);
            SetButtonInteractable(mediaExitFullscreenButton, enabled);
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
            AppendStreamStatusLog(message);

            if (streamBadgeText != null)
            {
                var stopped = message != null &&
                    (message.IndexOf("stopped", StringComparison.OrdinalIgnoreCase) >= 0 ||
                     message.IndexOf("关闭", StringComparison.OrdinalIgnoreCase) >= 0);
                if (stopped || !IsStreamActive())
                {
                    streamBadgeText.text = "串流关闭";
                    streamBadgeText.color = Theme.MutedText;
                }
                else
                {
                    streamBadgeText.text = "串流开启";
                    streamBadgeText.color = Theme.SuccessText;
                }
            }
        }

        private void AppendStreamStatusLog(string message)
        {
            if (string.IsNullOrEmpty(message))
            {
                return;
            }

            streamStatusLog.Add(DateTime.Now.ToString("HH:mm:ss") + "  " + message);
            while (streamStatusLog.Count > StreamStatusLogLimit)
            {
                streamStatusLog.RemoveAt(0);
            }

            if (streamLogModal != null && streamLogModal.activeSelf)
            {
                UpdateStreamLogText();
            }
        }

        private void UpdateStreamLogText()
        {
            if (streamLogText == null)
            {
                return;
            }

            streamLogText.text = streamStatusLog.Count == 0
                ? "暂无串流日志"
                : string.Join("\n", streamStatusLog);
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

        private static GameObject CreateCard(Transform parent, string name)
        {
            return CreateCard(parent, name, Theme.Card, 22);
        }

        private static GameObject CreateCard(Transform parent, string name, Color color, float radius)
        {
            var card = CreatePanel(parent, name, color);
            ApplyRounded(card.GetComponent<Image>(), Mathf.RoundToInt(radius));
            var outline = card.AddComponent<Outline>();
            outline.effectColor = Theme.Border;
            outline.effectDistance = new Vector2(1, -1);
            return card;
        }

        private static Text CreateCardTitle(Transform parent, string title, float height)
        {
            var text = CreateText(parent, title, 20, FontStyle.Bold, TextAnchor.MiddleLeft, height);
            text.color = Theme.PrimaryText;
            return text;
        }

        private static Text CreateLabel(Transform parent, string label)
        {
            var text = CreateText(parent, label, 12, FontStyle.Normal, TextAnchor.MiddleLeft, 18);
            text.color = Theme.MutedText;
            return text;
        }

        private static Text CreatePill(Transform parent, string label, Color textColor, float width)
        {
            var pill = CreateCard(parent, "Pill " + label, Theme.Pill, 17);
            var layout = pill.AddComponent<LayoutElement>();
            layout.preferredWidth = width;
            layout.minWidth = width;
            layout.preferredHeight = 34;
            layout.minHeight = 34;
            var text = CreateText(pill.transform, label, 13, FontStyle.Normal, TextAnchor.MiddleCenter, 0);
            text.color = textColor;
            Stretch(text.rectTransform, 12, 12, 0, 0);
            return text;
        }

        private static GameObject CreateLogo(Transform parent)
        {
            var logo = CreateUiObject("RCViewer Logo", parent);
            var rect = logo.GetComponent<RectTransform>();
            rect.sizeDelta = new Vector2(44, 44);

            var image = logo.AddComponent<Image>();
            image.sprite = RcViewerLogoSprite();
            image.type = Image.Type.Simple;
            image.preserveAspect = true;
            image.raycastTarget = false;
            return logo;
        }

        private static void ApplyLogoLayout(GameObject logo)
        {
            var layout = logo.GetComponent<LayoutElement>() ?? logo.AddComponent<LayoutElement>();
            layout.preferredWidth = 44;
            layout.minWidth = 44;
            layout.preferredHeight = 44;
            layout.minHeight = 44;
            layout.flexibleWidth = 0;
            layout.flexibleHeight = 0;
        }

        private static void CreateSpacer(Transform parent, float height)
        {
            var spacer = CreateUiObject("Spacer", parent);
            var element = spacer.AddComponent<LayoutElement>();
            element.minHeight = height;
            element.preferredHeight = height;
            element.flexibleHeight = 0;
        }

        private static Button CreatePrimaryButton(Transform parent, string label, Action onClick, float width)
        {
            return CreateStyledButton(parent, label, onClick, width, Theme.PrimaryButton, false);
        }

        private static Button CreateSecondaryButton(Transform parent, string label, Action onClick, float width)
        {
            return CreateStyledButton(parent, label, onClick, width, Theme.SecondaryButton, true);
        }

        private static Button CreatePillButton(Transform parent, string label, Action onClick, float width)
        {
            return CreateStyledButton(parent, label, onClick, width, Theme.Pill, false, 17, 13);
        }

        private static Button CreateRoundButton(Transform parent, string label, Action onClick, bool primary)
        {
            var button = CreateStyledButton(
                parent,
                label,
                onClick,
                64,
                primary ? Theme.PrimaryButton : Theme.SecondaryButton,
                !primary,
                32,
                14);
            var layout = button.GetComponent<LayoutElement>();
            layout.preferredHeight = 64;
            layout.minHeight = 64;
            layout.preferredWidth = 64;
            layout.minWidth = 64;
            layout.flexibleWidth = 0;
            layout.flexibleHeight = 0;
            return button;
        }

        private static Button CreateDangerRoundButton(Transform parent, string label, Action onClick)
        {
            var button = CreateStyledButton(parent, label, onClick, 64, Theme.Danger, true, 32, 14);
            var layout = button.GetComponent<LayoutElement>();
            layout.preferredHeight = 64;
            layout.minHeight = 64;
            layout.preferredWidth = 64;
            layout.minWidth = 64;
            layout.flexibleWidth = 0;
            layout.flexibleHeight = 0;
            return button;
        }

        private static Button CreateStyledButton(
            Transform parent,
            string label,
            Action onClick,
            float width,
            Color color,
            bool outline,
            float radius = 12,
            int fontSize = 14)
        {
            var root = CreatePanel(parent, "Button " + label, color);
            ApplyRounded(root.GetComponent<Image>(), Mathf.RoundToInt(radius));
            var layout = root.AddComponent<LayoutElement>();
            layout.preferredWidth = width;
            layout.minWidth = width;
            layout.preferredHeight = 42;
            layout.minHeight = 42;
            layout.flexibleWidth = 0;
            layout.flexibleHeight = 0;
            if (outline)
            {
                var border = root.AddComponent<Outline>();
                border.effectColor = Theme.Border;
                border.effectDistance = new Vector2(1, -1);
            }

            var button = root.AddComponent<Button>();
            button.targetGraphic = root.GetComponent<Image>();
            button.onClick.AddListener(() => onClick());
            var colors = button.colors;
            colors.normalColor = color;
            colors.highlightedColor = LerpColor(color, Color.white, 0.12f);
            colors.pressedColor = LerpColor(color, Color.black, 0.18f);
            colors.disabledColor = new Color(0.12f, 0.15f, 0.2f, 0.75f);
            button.colors = colors;

            var text = CreateText(root.transform, label, fontSize, FontStyle.Bold, TextAnchor.MiddleCenter, 0);
            text.color = Theme.PrimaryText;
            text.resizeTextForBestFit = true;
            text.resizeTextMinSize = 10;
            text.resizeTextMaxSize = fontSize;
            Stretch(text.rectTransform, 8, 8, 0, 0);
            return button;
        }

        private Button CreateServerRow(
            Transform parent,
            string serverName,
            string address,
            bool online,
            bool highlighted,
            Action editAction,
            Action connectAction)
        {
            var row = CreateCard(parent, "Server Row", highlighted ? Theme.RowHighlight : Theme.Background, 18);
            var rowElement = row.AddComponent<LayoutElement>();
            rowElement.minHeight = 88;
            rowElement.preferredHeight = 88;
            rowElement.flexibleHeight = 0;
            var layout = row.AddComponent<HorizontalLayoutGroup>();
            layout.padding = new RectOffset(18, 18, 14, 14);
            layout.spacing = 14;
            layout.childAlignment = TextAnchor.MiddleLeft;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = false;
            layout.childForceExpandHeight = false;

            var info = CreateUiObject("Server Info", row.transform);
            var infoElement = info.AddComponent<LayoutElement>();
            infoElement.flexibleWidth = 1;
            infoElement.minWidth = 180;
            var infoLayout = info.AddComponent<VerticalLayoutGroup>();
            infoLayout.spacing = 2;
            infoLayout.childControlWidth = true;
            infoLayout.childControlHeight = true;
            infoLayout.childForceExpandWidth = true;
            infoLayout.childForceExpandHeight = false;

            var title = CreateText(info.transform, highlighted ? "默认 · " + serverName : serverName, 15, FontStyle.Bold, TextAnchor.MiddleLeft, 26);
            title.color = Theme.PrimaryText;
            var meta = CreateText(info.transform, address + "  ·  " + (online ? "在线" : "离线"), 13, FontStyle.Normal, TextAnchor.MiddleLeft, 22);
            meta.color = online ? Theme.SuccessText : Theme.MutedText;

            var actions = CreateCompactRow(row.transform, "Server Row Actions", 42);
            var actionsElement = actions.GetComponent<LayoutElement>();
            actionsElement.preferredWidth = 176;
            actionsElement.minWidth = 176;
            actionsElement.flexibleWidth = 0;
            var actionsLayout = actions.GetComponent<HorizontalLayoutGroup>();
            actionsLayout.spacing = 10;
            actionsLayout.childAlignment = TextAnchor.MiddleCenter;

            CreateSecondaryButton(actions.transform, "编辑", editAction, 78);
            var connectButton = CreatePrimaryButton(actions.transform, "连接", connectAction, 78);
            connectButton.interactable = online;
            return connectButton;
        }

        private static void ClearChildren(Transform parent)
        {
            for (var i = parent.childCount - 1; i >= 0; i--)
            {
                UnityEngine.Object.Destroy(parent.GetChild(i).gameObject);
            }
        }

        private static Color LerpColor(Color a, Color b, float t)
        {
            return new Color(
                Mathf.Lerp(a.r, b.r, t),
                Mathf.Lerp(a.g, b.g, t),
                Mathf.Lerp(a.b, b.b, t),
                Mathf.Lerp(a.a, b.a, t));
        }

        private static void ApplyRounded(Image image, int radius)
        {
            if (image == null || radius <= 0)
            {
                return;
            }

            image.sprite = RoundedSprite(radius);
            image.type = Image.Type.Sliced;
        }

        private static Sprite rcViewerLogoSprite;
        private static readonly Dictionary<int, Sprite> RoundedSpriteCache = new Dictionary<int, Sprite>();

        private static Sprite RcViewerLogoSprite()
        {
            if (rcViewerLogoSprite != null)
            {
                return rcViewerLogoSprite;
            }

            const int logicalSize = 44;
            const int scale = 4;
            var size = logicalSize * scale;
            var pixels = new Color32[size * size];
            var transparent = new Color32(0, 0, 0, 0);
            var background = new Color32(0x09, 0x24, 0x3e, 0xff);
            var accent = new Color32(0x2d, 0xe2, 0xe6, 0xff);
            var blue = new Color32(0x4d, 0x8d, 0xff, 0xff);

            for (var y = 0; y < size; y++)
            {
                for (var x = 0; x < size; x++)
                {
                    var px = (x + 0.5f) / scale;
                    var py = logicalSize - (y + 0.5f) / scale;
                    var color = transparent;
                    var outer = IsInsideRoundedRect(px, py, 0f, 0f, 44f, 44f, 12f);
                    if (outer)
                    {
                        var inner = IsInsideRoundedRect(px, py, 1f, 1f, 42f, 42f, 11f);
                        color = inner ? background : accent;
                    }

                    if (IsInsideLogoRing(px, py))
                    {
                        color = accent;
                    }

                    if (IsInsideLogoPlay(px, py))
                    {
                        color = blue;
                    }

                    pixels[y * size + x] = color;
                }
            }

            var texture = new Texture2D(size, size, TextureFormat.ARGB32, false);
            texture.name = "RCViewerLogo";
            texture.wrapMode = TextureWrapMode.Clamp;
            texture.filterMode = FilterMode.Bilinear;
            texture.SetPixels32(pixels);
            texture.Apply();

            rcViewerLogoSprite = Sprite.Create(
                texture,
                new Rect(0, 0, size, size),
                new Vector2(0.5f, 0.5f),
                100f);
            return rcViewerLogoSprite;
        }

        private static bool IsInsideRoundedRect(float x, float y, float left, float top, float width, float height, float radius)
        {
            var right = left + width;
            var bottom = top + height;
            if (x < left || x > right || y < top || y > bottom)
            {
                return false;
            }

            var cx = Mathf.Clamp(x, left + radius, right - radius);
            var cy = Mathf.Clamp(y, top + radius, bottom - radius);
            var dx = x - cx;
            var dy = y - cy;
            return dx * dx + dy * dy <= radius * radius;
        }

        private static bool IsInsideLogoRing(float x, float y)
        {
            var dx = x - 22f;
            var dy = y - 20f;
            var distanceSquared = dx * dx + dy * dy;
            return distanceSquared <= 12f * 12f && distanceSquared >= 9f * 9f;
        }

        private static bool IsInsideLogoPlay(float x, float y)
        {
            var dx = x - 22f;
            if (dx < 0f || dx > 11f)
            {
                return false;
            }

            var halfHeight = 7f * (1f - dx / 11f);
            return Mathf.Abs(y - 20f) <= halfHeight;
        }

        private static Sprite RoundedSprite(int radius)
        {
            Sprite sprite;
            if (RoundedSpriteCache.TryGetValue(radius, out sprite))
            {
                return sprite;
            }

            var size = Mathf.Max(16, radius * 2 + 4);
            var texture = new Texture2D(size, size, TextureFormat.ARGB32, false);
            texture.name = "RemoteExplorerRounded" + radius;
            texture.wrapMode = TextureWrapMode.Clamp;
            var pixels = new Color32[size * size];
            var centerMin = radius;
            var centerMax = size - radius - 1;
            for (var y = 0; y < size; y++)
            {
                for (var x = 0; x < size; x++)
                {
                    var cx = Mathf.Clamp(x, centerMin, centerMax);
                    var cy = Mathf.Clamp(y, centerMin, centerMax);
                    var distance = Vector2.Distance(new Vector2(x, y), new Vector2(cx, cy));
                    pixels[y * size + x] = distance <= radius ? new Color32(255, 255, 255, 255) : new Color32(255, 255, 255, 0);
                }
            }
            texture.SetPixels32(pixels);
            texture.Apply();
            sprite = Sprite.Create(
                texture,
                new Rect(0, 0, size, size),
                new Vector2(0.5f, 0.5f),
                100f,
                0,
                SpriteMeshType.FullRect,
                new Vector4(radius, radius, radius, radius));
            RoundedSpriteCache[radius] = sprite;
            return sprite;
        }

        private static GameObject CreatePage(
            Transform parent,
            string name,
            int horizontalPadding,
            int verticalPadding,
            float spacing)
        {
            var page = CreatePanel(parent, name, Theme.Background);
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
            layout.childAlignment = TextAnchor.MiddleLeft;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = forceExpandWidth;
            layout.childForceExpandHeight = false;
            var element = row.AddComponent<LayoutElement>();
            element.minHeight = height;
            element.preferredHeight = height;
            element.flexibleHeight = 0;
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
            text.color = Theme.PrimaryText;
            text.horizontalOverflow = HorizontalWrapMode.Wrap;
            text.verticalOverflow = VerticalWrapMode.Truncate;
            var element = textObject.AddComponent<LayoutElement>();
            element.minHeight = height;
            element.preferredHeight = height;
            return text;
        }

        private static InputField CreateInput(Transform parent, string placeholder, float height, bool password)
        {
            var root = CreateCard(parent, "Input", Theme.Background, 10);
            var layout = root.AddComponent<LayoutElement>();
            layout.minHeight = height;
            layout.preferredHeight = height;
            layout.flexibleWidth = 1;
            var field = root.AddComponent<InputField>();
            field.contentType = password ? InputField.ContentType.Password : InputField.ContentType.Standard;

            var text = CreateText(root.transform, string.Empty, 14, FontStyle.Normal, TextAnchor.MiddleLeft, height);
            text.color = Theme.PrimaryText;
            Stretch(text.rectTransform, 14, 14, 0, 0);
            field.textComponent = text;

            var placeholderText = CreateText(root.transform, placeholder, 14, FontStyle.Normal, TextAnchor.MiddleLeft, height);
            placeholderText.color = Theme.MutedText;
            Stretch(placeholderText.rectTransform, 14, 14, 0, 0);
            field.placeholder = placeholderText;

            return field;
        }

        private static Button CreateButton(Transform parent, string label, Action onClick, float width)
        {
            return CreatePrimaryButton(parent, label, onClick, width);
        }

        private static Button CreateCompactButton(Transform parent, string label, Action onClick, float width)
        {
            var button = CreateButton(parent, label, onClick, width);
            var text = button.GetComponentInChildren<Text>();
            if (text != null)
            {
                text.fontSize = 14;
                text.resizeTextForBestFit = true;
                text.resizeTextMinSize = 10;
                text.resizeTextMaxSize = 14;
            }

            return button;
        }

        private static Dropdown CreateDropdown(Transform parent)
        {
            var root = CreateCard(parent, "Dropdown", Theme.Background, 10);
            var dropdown = root.AddComponent<Dropdown>();
            dropdown.targetGraphic = root.GetComponent<Image>();

            var label = CreateText(root.transform, string.Empty, 14, FontStyle.Normal, TextAnchor.MiddleLeft, 0);
            label.horizontalOverflow = HorizontalWrapMode.Overflow;
            label.verticalOverflow = VerticalWrapMode.Truncate;
            label.resizeTextForBestFit = true;
            label.resizeTextMinSize = 12;
            label.resizeTextMaxSize = 14;
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
            var template = CreateCard(parent, "Template", Theme.Card, 12);
            template.SetActive(false);
            var rect = template.GetComponent<RectTransform>();
            rect.pivot = new Vector2(0.5f, 1f);
            Anchor(rect, new Vector2(0, 0), new Vector2(1, 0), new Vector2(0, -4), new Vector2(0, 220));

            var scroll = template.AddComponent<ScrollRect>();
            scroll.horizontal = false;
            scroll.vertical = true;
            scroll.movementType = ScrollRect.MovementType.Clamped;
            scroll.scrollSensitivity = 18f;

            var viewport = CreatePanel(template.transform, "Viewport", Theme.Card);
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

            var item = CreatePanel(content.transform, "Item", Theme.SecondaryButton);
            var itemLayout = item.AddComponent<LayoutElement>();
            itemLayout.minHeight = 42;
            itemLayout.preferredHeight = 42;
            var toggle = item.AddComponent<Toggle>();
            var itemText = CreateText(item.transform, "Option", 14, FontStyle.Normal, TextAnchor.MiddleLeft, 0);
            itemText.fontSize = 14;
            itemText.resizeTextForBestFit = true;
            itemText.resizeTextMinSize = 11;
            itemText.resizeTextMaxSize = 14;
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
            var row = CreateRow(parent, "Toggle Row", height, false);
            var rowLayout = row.GetComponent<HorizontalLayoutGroup>();
            rowLayout.childAlignment = TextAnchor.MiddleLeft;
            rowLayout.childForceExpandHeight = false;
            var labelText = CreateText(row.transform, label, 13, FontStyle.Normal, TextAnchor.MiddleLeft, height);
            labelText.color = Theme.PrimaryText;
            labelText.GetComponent<LayoutElement>().flexibleWidth = 1;
            var box = CreateCard(row.transform, "Toggle Box", Theme.Pill, 12);
            box.GetComponent<RectTransform>().sizeDelta = new Vector2(42, 24);
            var boxLayout = box.AddComponent<LayoutElement>();
            boxLayout.preferredWidth = 42;
            boxLayout.minWidth = 42;
            boxLayout.preferredHeight = 24;
            boxLayout.minHeight = 24;
            boxLayout.flexibleWidth = 0;
            boxLayout.flexibleHeight = 0;
            var toggle = box.AddComponent<Toggle>();
            toggle.targetGraphic = box.GetComponent<Image>();
            var check = CreateCard(box.transform, "Checkmark", Theme.SuccessText, 9);
            Anchor(check.GetComponent<RectTransform>(), new Vector2(1, 0.5f), new Vector2(1, 0.5f), new Vector2(-12, 0), new Vector2(18, 18));
            toggle.graphic = check.GetComponent<Image>();
            return toggle;
        }

        private static Slider CreateSlider(Transform parent, float minValue, float maxValue, float value, float width)
        {
            var root = CreatePanel(parent, "Slider", new Color(0f, 0f, 0f, 0f));
            var rootLayout = root.AddComponent<LayoutElement>();
            rootLayout.preferredWidth = width;
            rootLayout.flexibleWidth = 1;

            var slider = root.AddComponent<Slider>();
            slider.minValue = minValue;
            slider.maxValue = maxValue;
            slider.value = value;
            slider.wholeNumbers = true;

            var background = CreateCard(root.transform, "Background", Theme.Border, 4);
            Anchor(background.GetComponent<RectTransform>(), new Vector2(0, 0.5f), new Vector2(1, 0.5f), Vector2.zero, new Vector2(-34, 8));

            var fillArea = CreateUiObject("Fill Area", root.transform);
            Stretch(fillArea.GetComponent<RectTransform>(), 18, 18, 0, 0);
            var fill = CreateCard(fillArea.transform, "Fill", Theme.Accent, 4);
            Stretch(fill.GetComponent<RectTransform>(), 0, 0, 20, 20);

            var handleArea = CreateUiObject("Handle Slide Area", root.transform);
            Stretch(handleArea.GetComponent<RectTransform>(), 18, 18, 0, 0);
            var handle = CreateCard(handleArea.transform, "Handle", Theme.PrimaryText, 11);
            Anchor(handle.GetComponent<RectTransform>(), new Vector2(0, 0.5f), new Vector2(0, 0.5f), Vector2.zero, new Vector2(22, 22));

            slider.fillRect = fill.GetComponent<RectTransform>();
            slider.handleRect = handle.GetComponent<RectTransform>();
            slider.targetGraphic = handle.GetComponent<Image>();
            return slider;
        }

        private void CreateStreamPreview(Transform parent)
        {
            var previewPanel = CreatePanel(parent, "Streaming Viewport", new Color(0f, 0f, 0f, 0f));
            streamPreviewPanel = previewPanel;
            var previewImage = previewPanel.GetComponent<Image>();
            if (previewImage != null)
            {
                previewImage.raycastTarget = false;
            }

            var previewLayout = previewPanel.AddComponent<LayoutElement>();
            previewLayout.flexibleWidth = 1;
            previewLayout.minWidth = 560;
            previewLayout.preferredWidth = 900;
            previewLayout.flexibleHeight = 1;
            previewLayout.minHeight = RemotePreviewMinHeight;
            previewLayout.preferredHeight = RemotePreviewPreferredHeight;

            var panelLayout = previewPanel.AddComponent<VerticalLayoutGroup>();
            panelLayout.padding = new RectOffset(0, 0, 0, 0);
            panelLayout.spacing = 12;
            panelLayout.childControlWidth = true;
            panelLayout.childControlHeight = true;
            panelLayout.childForceExpandWidth = true;
            panelLayout.childForceExpandHeight = false;

            var viewport = CreatePanel(previewPanel.transform, "Stream Image Surface", new Color(0f, 0f, 0f, 0f));
            var viewportImage = viewport.GetComponent<Image>();
            if (viewportImage != null)
            {
                viewportImage.raycastTarget = false;
            }

            var viewportElement = viewport.AddComponent<LayoutElement>();
            viewportElement.flexibleWidth = 1;
            viewportElement.flexibleHeight = 1;
            viewportElement.minHeight = RemotePreviewMinHeight;
            viewportElement.preferredHeight = RemotePreviewPreferredHeight;

            var previewObject = CreateUiObject("Stream Image", viewport.transform);
            Stretch(previewObject.GetComponent<RectTransform>(), 0, 0, 0, 0);
            streamImage = previewObject.AddComponent<RawImage>();
            streamImage.color = new Color(1f, 1f, 1f, 0f);
            streamImage.raycastTarget = true;
            streamImage.uvRect = new Rect(0f, 0f, 1f, 1f);

            var actionRow = CreateCompactRow(previewPanel.transform, "Stream Action Row", 44);
            var actionLayout = actionRow.GetComponent<HorizontalLayoutGroup>();
            actionLayout.spacing = 12;
            actionLayout.childAlignment = TextAnchor.MiddleRight;
            var spacer = CreateUiObject("Stream Action Spacer", actionRow.transform);
            spacer.AddComponent<LayoutElement>().flexibleWidth = 1;
            streamLogButton = CreateSecondaryButton(actionRow.transform, "日志", ToggleStreamLogModal, 76);
            streamToggleButton = CreateSecondaryButton(actionRow.transform, "打开串流", () => FireAndForget(ToggleStreamAsync), 112);
            streamFullscreenButton = CreatePrimaryButton(actionRow.transform, "全屏展示", ToggleStreamPreviewFullscreen, 112);

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
                return string.IsNullOrEmpty(settings.StreamResolution) ? "720p" : settings.StreamResolution;
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
                streamFpsText.text = SelectedStreamFps() + " FPS";
            }
        }

        private void ScheduleStreamSettingsApply()
        {
            UpdateStreamFpsLabel();
            settings.StreamMode = StreamModePreference(SelectedStreamMode());
            settings.StreamResolution = SelectedStreamResolution();
            settings.StreamFps = SelectedStreamFps();
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

        private static class Theme
        {
            public static readonly Color Background = new Color32(0x07, 0x11, 0x1f, 0xff);
            public static readonly Color Card = new Color32(0x0d, 0x1b, 0x2e, 0xff);
            public static readonly Color Modal = new Color32(0x10, 0x23, 0x3a, 0xff);
            public static readonly Color StreamBackground = new Color32(0x03, 0x08, 0x11, 0xff);
            public static readonly Color Border = new Color(0x21 / 255f, 0x39 / 255f, 0x57 / 255f, 0.9f);
            public static readonly Color PrimaryButton = new Color32(0x4d, 0x8d, 0xff, 0xff);
            public static readonly Color SecondaryButton = new Color32(0x15, 0x2b, 0x46, 0xff);
            public static readonly Color Pill = new Color32(0x15, 0x2b, 0x46, 0xff);
            public static readonly Color RowHighlight = new Color32(0x13, 0x2b, 0x49, 0xff);
            public static readonly Color Danger = new Color32(0xff, 0x5c, 0x7a, 0xb8);
            public static readonly Color PrimaryText = new Color32(0xe7, 0xf3, 0xff, 0xff);
            public static readonly Color MutedText = new Color32(0x8b, 0xa4, 0xbd, 0xff);
            public static readonly Color SuccessText = new Color32(0x28, 0xe5, 0x9d, 0xff);
            public static readonly Color Accent = new Color32(0x2d, 0xe2, 0xe6, 0xff);
        }
    }
}
