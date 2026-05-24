using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RemoteExplorer
{
    public class RemoteExplorerClient : IDisposable
    {
        private UdpClient controlClient;
        private IPEndPoint controlEndpoint;
        private string sessionId;
        private byte[] sessionKey;
        private long commandCounter;
        private readonly SemaphoreSlim requestLock = new SemaphoreSlim(1, 1);
        private readonly object streamLock = new object();
        private readonly Dictionary<uint, RemoteStreamFrameBuilder> partialStreamFrames =
            new Dictionary<uint, RemoteStreamFrameBuilder>();
        private RemoteStreamFrame latestCompletedStreamFrame;
        private bool hasCompletedStreamFrame;
        private uint latestCompletedFrameId;
        private bool hasNewestObservedFrameId;
        private uint newestObservedFrameId;
        private UdpClient streamClient;
        private CancellationTokenSource streamCancellation;
        private Task streamReceiveTask;
        private int streamPort;
        private int streamGeneration;
        private DateTime streamStatsSinceUtc;
        private DateTime nextStreamStatsLogUtc;
        private long streamStatsPackets;
        private long streamStatsBytes;
        private long streamStatsFrames;
        private long streamStatsFrameGaps;
        private long streamStatsPartialsCreated;
        private long streamStatsPartialPruned;
        private long streamStatsInvalidPackets;
        private long streamStatsInvalidChunks;
        private long streamStatsGenerationDrops;
        private long streamStatsBehindPackets;
        private long streamStatsObsoletePackets;
        private long streamStatsMismatchedFrames;
        private long streamStatsChunkAddFailures;
        private uint streamStatsLastCompletedFrameId;
        private bool streamStatsHasCompletedFrameId;
        private int consecutiveControlTimeouts;

        private static readonly byte[] StreamMagic = Encoding.ASCII.GetBytes("REXPSTR1");
        private const int StreamHeaderBytes = 24;
        private const int StreamChunkBytes = 1000;
        private const int MaxStreamChunks = 512;
        private const int StreamReceiveBufferBytes = 8 * 1024 * 1024;
        private const int MaxReceiveBurstPackets = 1024;
        private const int SioUdpConnectionReset = -1744830452;
        private const double StreamStatsIntervalSeconds = 5.0;
        private const float DefaultControlTimeoutSeconds = 12f;
        private const int ConsecutiveControlTimeoutLimit = 3;
        private const float WebRtcOfferTimeoutSeconds = 15f;
        private const float WebRtcStopTimeoutSeconds = 8f;
        public const int DefaultStreamFps = 30;
        public const int MinStreamFps = 20;
        public const int MaxStreamFps = 60;
        public const int DefaultStreamQuality = 55;

        public event Action<DiscoveredServer, string> ConnectionLost;

        public bool IsConnected => controlClient != null && !string.IsNullOrEmpty(sessionId);
        public bool IsStreaming => streamClient != null;
        public DiscoveredServer ConnectedServer { get; private set; }

        public async Task<List<DiscoveredServer>> DiscoverAsync(
            int discoveryPort = RemoteExplorerProtocol.DefaultDiscoveryPort,
            float timeoutSeconds = 2.0f,
            CancellationToken cancellationToken = default)
        {
            var result = new List<DiscoveredServer>();
            var seen = new HashSet<string>();

            using (var udp = CreateDiscoveryListener(discoveryPort))
            {
                var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
                while (DateTime.UtcNow < deadline && !cancellationToken.IsCancellationRequested)
                {
                    var remaining = deadline - DateTime.UtcNow;
                    if (remaining <= TimeSpan.Zero)
                    {
                        break;
                    }

                    var receiveTask = udp.ReceiveAsync();
                    var delayTask = Task.Delay(remaining, cancellationToken);
                    var completed = await Task.WhenAny(receiveTask, delayTask);
                    if (completed != receiveTask)
                    {
                        break;
                    }

                    UdpReceiveResult received;
                    try
                    {
                        received = await receiveTask;
                    }
                    catch (SocketException)
                    {
                        continue;
                    }

                    var json = Encoding.UTF8.GetString(received.Buffer);
                    ServerOfferEnvelope offer;
                    try
                    {
                        offer = JsonUtility.FromJson<ServerOfferEnvelope>(json);
                    }
                    catch
                    {
                        continue;
                    }

                    if (offer == null || offer.type != "offer" || offer.server == null)
                    {
                        continue;
                    }

                    var server = new DiscoveredServer
                    {
                        Address = received.RemoteEndPoint.Address.ToString(),
                        ControlPort = offer.server.control_port > 0 ? offer.server.control_port : RemoteExplorerProtocol.DefaultControlPort,
                        Id = offer.server.id,
                        Name = offer.server.name,
                        AuthMode = offer.server.auth,
                        Capabilities = offer.server.capabilities ?? new string[0]
                    };

                    var key = string.Format("{0}:{1}:{2}", server.Address, server.ControlPort, server.Id);
                    if (seen.Add(key))
                    {
                        result.Add(server);
                    }
                }
            }

            return result;
        }

        private static UdpClient CreateDiscoveryListener(int discoveryPort)
        {
            var udp = new UdpClient(AddressFamily.InterNetwork);
            udp.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            udp.Client.Bind(new IPEndPoint(IPAddress.Any, discoveryPort));
            return udp;
        }

        public async Task ConnectAsync(DiscoveredServer server, string password, CancellationToken cancellationToken = default)
        {
            Disconnect();
            controlEndpoint = new IPEndPoint(IPAddress.Parse(server.Address), server.ControlPort);
            controlClient = new UdpClient(0);
            ConfigureStreamSocket(controlClient.Client);
            ConnectedServer = server;

            var clientId = RemoteExplorerProtocol.ClientId();
            var hello = new Dictionary<string, object>
            {
                ["v"] = RemoteExplorerProtocol.Version,
                ["type"] = "auth_hello",
                ["request_id"] = RemoteExplorerProtocol.NewRequestId(),
                ["client"] = new Dictionary<string, object>
                {
                    ["id"] = clientId,
                    ["name"] = RemoteExplorerProtocol.ClientName()
                }
            };

            var auth = await SendRequestAsync<AuthEnvelope>(hello, cancellationToken);
            if (auth.type == "auth_ok")
            {
                sessionId = auth.session.id;
                sessionKey = null;
                commandCounter = 0;
                ResetControlTimeouts();
                return;
            }

            if (auth.type != "auth_challenge" || auth.challenge == null)
            {
                throw new InvalidOperationException("Unexpected authentication response.");
            }

            if (string.IsNullOrEmpty(password))
            {
                throw new InvalidOperationException("This server requires a password.");
            }

            var challenge = auth.challenge;
            var clientNonce = RemoteExplorerProtocol.NewRequestId();
            var passwordKey = RemoteExplorerProtocol.DerivePasswordKey(
                password,
                challenge.salt,
                challenge.iterations > 0 ? challenge.iterations : RemoteExplorerProtocol.Pbkdf2Iterations);
            var proof = RemoteExplorerProtocol.HmacHex(
                passwordKey,
                RemoteExplorerProtocol.ProofMessage(clientId, challenge.server_nonce, clientNonce));
            var authResponse = new Dictionary<string, object>
            {
                ["v"] = RemoteExplorerProtocol.Version,
                ["type"] = "auth_response",
                ["request_id"] = RemoteExplorerProtocol.NewRequestId(),
                ["client_id"] = clientId,
                ["server_nonce"] = challenge.server_nonce,
                ["client_nonce"] = clientNonce,
                ["proof"] = proof
            };

            var authOk = await SendRequestAsync<AuthEnvelope>(authResponse, cancellationToken);
            if (authOk.type != "auth_ok" || authOk.session == null)
            {
                throw new InvalidOperationException("Authentication failed.");
            }

            sessionId = authOk.session.id;
            sessionKey = RemoteExplorerProtocol.HmacBytes(
                passwordKey,
                RemoteExplorerProtocol.SessionMessage(clientId, challenge.server_nonce, clientNonce));
            commandCounter = 0;
            ResetControlTimeouts();
        }

        public async Task<CommandEnvelope> NavigateAsync(string url, CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync("navigate", new Dictionary<string, object> { ["url"] = url }, cancellationToken);
        }

        public async Task<CommandEnvelope> ClosePageAsync(CancellationToken cancellationToken = default)
        {
            var result = await SendCommandAsync("close_page", new Dictionary<string, object>(), cancellationToken);
            if (result.ok || result.error == null || result.error.code != "unknown_command")
            {
                return result;
            }

            return await NavigateAsync("about:blank", cancellationToken);
        }

        public async Task<CommandEnvelope> ClickAsync(
            float x,
            float y,
            int sourceWidth = 0,
            int sourceHeight = 0,
            CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync(
                "click",
                new Dictionary<string, object>
                {
                    ["x"] = x,
                    ["y"] = y,
                    ["source_width"] = sourceWidth,
                    ["source_height"] = sourceHeight
                },
                cancellationToken);
        }

        public async Task<CommandEnvelope> ScrollAsync(
            float dx,
            float dy,
            CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync(
                "scroll",
                new Dictionary<string, object>
                {
                    ["dx"] = dx,
                    ["dy"] = dy
                },
                cancellationToken);
        }

        public async Task<CommandEnvelope> SetFocusedInputAsync(
            string text,
            bool submit = false,
            CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync(
                "set_input",
                new Dictionary<string, object>
                {
                    ["text"] = text ?? string.Empty,
                    ["submit"] = submit
                },
                cancellationToken);
        }

        public async Task<CommandEnvelope> SendTextAsync(
            string text,
            CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync(
                "text",
                new Dictionary<string, object>
                {
                    ["text"] = text ?? string.Empty
                },
                cancellationToken);
        }

        public async Task<CommandEnvelope> SendKeyAsync(
            string key,
            CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync(
                "key",
                new Dictionary<string, object>
                {
                    ["key"] = key ?? string.Empty
                },
                cancellationToken);
        }

        public async Task<CommandEnvelope> MediaStatusAsync(CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync("media_status", new Dictionary<string, object>(), cancellationToken);
        }

        public async Task<CommandEnvelope> MediaControlAsync(
            string action,
            float amount = 0f,
            CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync(
                "media_control",
                new Dictionary<string, object>
                {
                    ["action"] = action ?? string.Empty,
                    ["amount"] = amount
                },
                cancellationToken);
        }

        public async Task<CommandEnvelope> ClearRemoteCacheAsync(
            bool clearCookies,
            CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync(
                "clear_cache",
                new Dictionary<string, object>
                {
                    ["clear_cookies"] = clearCookies
                },
                cancellationToken);
        }

        public async Task<CommandEnvelope> StartStreamAsync(
            string resolution = "360p",
            int fps = DefaultStreamFps,
            int quality = DefaultStreamQuality,
            CancellationToken cancellationToken = default)
        {
            if (!IsConnected)
            {
                throw new InvalidOperationException("Client is not connected.");
            }

            StopLocalStream();
            streamClient = new UdpClient(0);
            ConfigureStreamSocket(streamClient.Client);

            var localEndpoint = streamClient.Client.LocalEndPoint as IPEndPoint;
            if (localEndpoint == null)
            {
                StopLocalStream();
                throw new InvalidOperationException("Could not open stream UDP port.");
            }

            streamPort = localEndpoint.Port;
            var streamHost = ResolveLocalAddressFor(controlEndpoint);
            RemoteExplorerDiagnostics.Info(
                $"UDP/JPEG stream local socket opened host={streamHost ?? "auto"} port={streamPort} resolution={resolution} fps={ClampStreamFps(fps)} quality={quality}");
            streamCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            // generation 用于丢弃旧 socket 关闭后迟到的 UDP 包。 / Generation drops late packets from an old socket.
            var localStreamGeneration = BeginLocalStreamState();
            streamReceiveTask = ReceiveStreamLoopAsync(streamClient, streamCancellation.Token, localStreamGeneration);

            var streamPayload = new Dictionary<string, object>
            {
                ["port"] = streamPort,
                ["resolution"] = resolution,
                ["fps"] = ClampStreamFps(fps),
                ["quality"] = quality
            };
            if (!string.IsNullOrEmpty(streamHost))
            {
                streamPayload["host"] = streamHost;
            }

            var result = await SendCommandAsync(
                "stream_start",
                streamPayload,
                cancellationToken);

            if (!result.ok && result.type != "result")
            {
                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                Debug.LogWarning("[RemoteExplorer] UDP/JPEG stream_start failed: " + error);
                StopLocalStream();
            }
            else
            {
                var serverSourcePort = result.result != null ? result.result.source_port : 0;
                var serverTargetHost = result.result != null ? result.result.host : string.Empty;
                var serverTargetPort = result.result != null ? result.result.port : 0;
                RemoteExplorerDiagnostics.Info(
                    $"UDP/JPEG stream_start accepted by server target={serverTargetHost}:{serverTargetPort} source_port={serverSourcePort}");
                await SendStreamFirewallPunchesAsync(serverSourcePort, cancellationToken);
            }

            return result;
        }

        public async Task<CommandEnvelope> ConfigureStreamAsync(
            string resolution = "360p",
            int fps = DefaultStreamFps,
            int quality = DefaultStreamQuality,
            CancellationToken cancellationToken = default)
        {
            if (!IsStreaming || streamPort <= 0)
            {
                return await StartStreamAsync(resolution, fps, quality, cancellationToken);
            }

            RemoteExplorerDiagnostics.Info(
                $"UDP/JPEG stream_config resolution={resolution} fps={ClampStreamFps(fps)} quality={quality} port={streamPort}");
            return await SendCommandAsync(
                "stream_config",
                new Dictionary<string, object>
                {
                    ["port"] = streamPort,
                    ["resolution"] = resolution,
                    ["fps"] = ClampStreamFps(fps),
                    ["quality"] = quality
                },
                cancellationToken);
        }

        public async Task<CommandEnvelope> StopStreamAsync(CancellationToken cancellationToken = default)
        {
            CommandEnvelope result = null;
            try
            {
                if (IsConnected)
                {
                    RemoteExplorerDiagnostics.Info("UDP/JPEG stream_stop requested.");
                    result = await SendCommandAsync("stream_stop", new Dictionary<string, object>(), cancellationToken);
                }
            }
            finally
            {
                StopLocalStream();
            }

            return result ?? new CommandEnvelope { type = "result", ok = true };
        }

        public bool TryDequeueStreamFrame(out RemoteStreamFrame frame)
        {
            lock (streamLock)
            {
                if (latestCompletedStreamFrame == null)
                {
                    frame = null;
                    return false;
                }

                frame = latestCompletedStreamFrame;
                latestCompletedStreamFrame = null;
                return true;
            }
        }

        public async Task<CommandEnvelope> BrowserCommandAsync(
            string command,
            CancellationToken cancellationToken = default,
            float timeoutSeconds = DefaultControlTimeoutSeconds,
            bool markConnectionLostOnFailure = true)
        {
            return await SendCommandAsync(
                command,
                new Dictionary<string, object>(),
                cancellationToken,
                timeoutSeconds,
                markConnectionLostOnFailure);
        }

        public async Task<CommandEnvelope> WebRtcOfferAsync(
            string sdp,
            string type,
            string resolution,
            int fps,
            CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync(
                "webrtc_offer",
                new Dictionary<string, object>
                {
                    ["sdp"] = sdp,
                    ["type"] = string.IsNullOrEmpty(type) ? "offer" : type,
                    ["resolution"] = resolution,
                    ["fps"] = ClampStreamFps(fps)
                },
                cancellationToken,
                WebRtcOfferTimeoutSeconds);
        }

        public async Task<CommandEnvelope> WebRtcStatusAsync(CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync("webrtc_status", new Dictionary<string, object>(), cancellationToken);
        }

        public async Task<CommandEnvelope> StopWebRtcAsync(string peerId, CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync(
                "webrtc_stop",
                new Dictionary<string, object> { ["peer_id"] = peerId ?? string.Empty },
                cancellationToken,
                WebRtcStopTimeoutSeconds);
        }

        public async Task<CommandEnvelope> SendCommandAsync(
            string command,
            Dictionary<string, object> payload,
            CancellationToken cancellationToken = default,
            float timeoutSeconds = DefaultControlTimeoutSeconds,
            bool markConnectionLostOnFailure = true)
        {
            if (!IsConnected)
            {
                throw new InvalidOperationException("Client is not connected.");
            }

            var auth = new Dictionary<string, object>
            {
                ["session_id"] = sessionId
            };
            var message = new Dictionary<string, object>
            {
                ["v"] = RemoteExplorerProtocol.Version,
                ["type"] = "command",
                ["request_id"] = RemoteExplorerProtocol.NewRequestId(),
                ["command"] = command,
                ["payload"] = payload ?? new Dictionary<string, object>(),
                ["auth"] = auth
            };

            if (sessionKey != null)
            {
                auth["counter"] = ++commandCounter;
                auth["signature"] = RemoteExplorerProtocol.SignMessage(message, sessionKey);
            }

            try
            {
                var result = await SendRequestAsync<CommandEnvelope>(message, cancellationToken, timeoutSeconds);
                ResetControlTimeouts();
                if (markConnectionLostOnFailure && IsConnectionInvalidError(result))
                {
                    MarkConnectionLost(result.error.message);
                }

                return result;
            }
            catch (Exception ex) when (IsConnectionFailure(ex, cancellationToken))
            {
                if (IsTimeoutException(ex))
                {
                    RecordControlTimeout(ex.Message);
                }
                else if (markConnectionLostOnFailure)
                {
                    MarkConnectionLost(ex.Message);
                }
                throw;
            }
        }

        public void MarkConnectionLostFromHealthCheck(string reason)
        {
            MarkConnectionLost(reason);
        }

        public void Disconnect()
        {
            DisconnectInternal();
        }

        private void DisconnectInternal()
        {
            StopLocalStream();
            sessionId = null;
            sessionKey = null;
            commandCounter = 0;
            ResetControlTimeouts();
            ConnectedServer = null;
            controlEndpoint = null;
            controlClient?.Close();
            controlClient?.Dispose();
            controlClient = null;
        }

        private void MarkConnectionLost(string reason)
        {
            if (!IsConnected && controlClient == null)
            {
                return;
            }

            var server = ConnectedServer;
            var message = string.IsNullOrWhiteSpace(reason) ? "Server connection lost." : reason;
            RemoteExplorerDiagnostics.Info("Server connection lost: " + message);
            DisconnectInternal();
            ConnectionLost?.Invoke(server, message);
        }

        private static bool IsConnectionInvalidError(CommandEnvelope result)
        {
            if (result == null || result.error == null)
            {
                return false;
            }

            return string.Equals(result.error.code, "invalid_session", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(result.error.code, "auth_required", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(result.error.code, "bad_signature", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(result.error.code, "replay", StringComparison.OrdinalIgnoreCase);
        }

        private static bool IsConnectionFailure(Exception ex, CancellationToken cancellationToken)
        {
            if (cancellationToken.IsCancellationRequested)
            {
                return false;
            }

            return IsConnectionFailureException(ex);
        }

        private static bool IsConnectionFailureException(Exception ex)
        {
            if (ex is AggregateException aggregate)
            {
                return aggregate.Flatten().InnerExceptions.Any(IsConnectionFailureException);
            }

            return ex is TimeoutException ||
                ex is SocketException ||
                ex is ObjectDisposedException;
        }

        private static bool IsTimeoutException(Exception ex)
        {
            if (ex is AggregateException aggregate)
            {
                return aggregate.Flatten().InnerExceptions.Any(IsTimeoutException);
            }

            return ex is TimeoutException;
        }

        private void RecordControlTimeout(string reason)
        {
            if (!IsConnected)
            {
                return;
            }

            consecutiveControlTimeouts++;
            RemoteExplorerDiagnostics.Info(
                $"Control timeout count={consecutiveControlTimeouts}/{ConsecutiveControlTimeoutLimit}: {reason}");
            if (consecutiveControlTimeouts >= ConsecutiveControlTimeoutLimit)
            {
                MarkConnectionLost(reason);
            }
        }

        private void ResetControlTimeouts()
        {
            consecutiveControlTimeouts = 0;
        }

        public void Dispose()
        {
            Disconnect();
            ConnectionLost = null;
            requestLock.Dispose();
        }

        private void StopLocalStream()
        {
            if (streamClient != null)
            {
                lock (streamLock)
                {
                    LogStreamReceiveStats("stop", true);
                }
            }

            streamCancellation?.Cancel();
            streamClient?.Close();
            streamClient?.Dispose();
            streamClient = null;
            streamPort = 0;
            streamReceiveTask = null;
            streamCancellation?.Dispose();
            streamCancellation = null;

            lock (streamLock)
            {
                streamGeneration++;
                ClearStreamFrameState();
            }
        }

        private int BeginLocalStreamState()
        {
            lock (streamLock)
            {
                streamGeneration++;
                ClearStreamFrameState();
                ResetStreamStats();
                return streamGeneration;
            }
        }

        private void ClearStreamFrameState()
        {
            partialStreamFrames.Clear();
            latestCompletedStreamFrame = null;
            latestCompletedFrameId = 0;
            hasCompletedStreamFrame = false;
            newestObservedFrameId = 0;
            hasNewestObservedFrameId = false;
        }

        private void ResetStreamStats()
        {
            var now = DateTime.UtcNow;
            streamStatsSinceUtc = now;
            nextStreamStatsLogUtc = now.AddSeconds(StreamStatsIntervalSeconds);
            streamStatsPackets = 0;
            streamStatsBytes = 0;
            streamStatsFrames = 0;
            streamStatsFrameGaps = 0;
            streamStatsPartialsCreated = 0;
            streamStatsPartialPruned = 0;
            streamStatsInvalidPackets = 0;
            streamStatsInvalidChunks = 0;
            streamStatsGenerationDrops = 0;
            streamStatsBehindPackets = 0;
            streamStatsObsoletePackets = 0;
            streamStatsMismatchedFrames = 0;
            streamStatsChunkAddFailures = 0;
            streamStatsLastCompletedFrameId = 0;
            streamStatsHasCompletedFrameId = false;
        }

        private async Task ReceiveStreamLoopAsync(
            UdpClient udp,
            CancellationToken cancellationToken,
            int generation)
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                UdpReceiveResult received;
                try
                {
                    received = await udp.ReceiveAsync();
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (SocketException)
                {
                    if (!cancellationToken.IsCancellationRequested)
                    {
                        Debug.LogWarning("[RemoteExplorer] Stream receive socket stopped.");
                    }

                    return;
                }
                catch (Exception ex)
                {
                    if (!cancellationToken.IsCancellationRequested)
                    {
                        Debug.LogWarning("[RemoteExplorer] Stream receive loop stopped: " + ex.Message);
                    }

                    return;
                }

                AcceptStreamPacket(received.Buffer, generation);
                DrainAvailableStreamPackets(udp, cancellationToken, generation);
            }
        }

        private void DrainAvailableStreamPackets(
            UdpClient udp,
            CancellationToken cancellationToken,
            int generation)
        {
            var packets = 0;
            while (!cancellationToken.IsCancellationRequested && packets < MaxReceiveBurstPackets)
            {
                try
                {
                    if (udp.Client == null || udp.Client.Available <= 0)
                    {
                        return;
                    }

                    var endpoint = new IPEndPoint(IPAddress.Any, 0);
                    AcceptStreamPacket(udp.Receive(ref endpoint), generation);
                    packets++;
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (SocketException)
                {
                    return;
                }
            }
        }

        private void AcceptStreamPacket(byte[] packet, int generation)
        {
            lock (streamLock)
            {
                // UDP 分片可能乱序/丢失；只保留最新帧相关分片，降低延迟。
                // UDP chunks can arrive out of order or be lost; keep only chunks for the newest frames.
                if (packet == null || packet.Length <= StreamHeaderBytes)
                {
                    streamStatsInvalidPackets++;
                    LogStreamReceiveStats("interval");
                    return;
                }

                streamStatsPackets++;
                streamStatsBytes += packet.Length;

                for (var i = 0; i < StreamMagic.Length; i++)
                {
                    if (packet[i] != StreamMagic[i])
                    {
                        streamStatsInvalidPackets++;
                        LogStreamReceiveStats("interval");
                        return;
                    }
                }

                var frameId = ReadUInt32(packet, 8);
                var chunkIndex = ReadUInt16(packet, 12);
                var chunkCount = ReadUInt16(packet, 14);
                var width = ReadUInt16(packet, 16);
                var height = ReadUInt16(packet, 18);
                var sourceWidth = ReadUInt16(packet, 20);
                var sourceHeight = ReadUInt16(packet, 22);

                if (chunkCount == 0 || chunkCount > MaxStreamChunks || chunkIndex >= chunkCount)
                {
                    streamStatsInvalidChunks++;
                    LogStreamReceiveStats("interval");
                    return;
                }

                if (generation != streamGeneration)
                {
                    streamStatsGenerationDrops++;
                    LogStreamReceiveStats("interval");
                    return;
                }

                if (MarkNewestObservedFrameId(frameId))
                {
                    // Keep recent partial frames; late UDP chunks can still complete them after newer frames arrive.
                    PrunePartialFrames();
                }
                else if (IsBehindNewestObservedFrameId(frameId))
                {
                    streamStatsBehindPackets++;
                }

                if (IsObsoleteFrameId(frameId))
                {
                    streamStatsObsoletePackets++;
                    LogStreamReceiveStats("interval");
                    return;
                }

                RemoteStreamFrameBuilder builder;
                if (!partialStreamFrames.TryGetValue(frameId, out builder))
                {
                    builder = new RemoteStreamFrameBuilder(
                        frameId,
                        chunkCount,
                        StreamChunkBytes,
                        width,
                        height,
                        sourceWidth,
                        sourceHeight);
                    partialStreamFrames[frameId] = builder;
                    streamStatsPartialsCreated++;
                }

                if (builder.ChunkCount != chunkCount ||
                    builder.Width != width ||
                    builder.Height != height ||
                    builder.SourceWidth != sourceWidth ||
                    builder.SourceHeight != sourceHeight)
                {
                    partialStreamFrames.Remove(frameId);
                    streamStatsMismatchedFrames++;
                    LogStreamReceiveStats("interval");
                    return;
                }

                bool completed;
                if (!builder.TryAddChunk(
                        chunkIndex,
                        packet,
                        StreamHeaderBytes,
                        packet.Length - StreamHeaderBytes,
                        out completed))
                {
                    streamStatsChunkAddFailures++;
                    LogStreamReceiveStats("interval");
                    return;
                }

                if (completed)
                {
                    var completedFrame = builder.Build();
                    partialStreamFrames.Remove(frameId);

                    if (!IsObsoleteFrameId(completedFrame.FrameId))
                    {
                        latestCompletedStreamFrame = completedFrame;
                        latestCompletedFrameId = completedFrame.FrameId;
                        hasCompletedStreamFrame = true;
                        RecordCompletedStreamFrame(completedFrame.FrameId);
                    }
                }

                PrunePartialFrames();
                LogStreamReceiveStats("interval");
            }
        }

        private void PrunePartialFrames()
        {
            var cutoff = DateTime.UtcNow.AddSeconds(-3);
            var stale = partialStreamFrames
                .Where(pair =>
                    pair.Value.LastUpdatedUtc < cutoff ||
                    IsObsoleteFrameId(pair.Key))
                .Select(pair => pair.Key)
                .ToList();
            foreach (var frameId in stale)
            {
                partialStreamFrames.Remove(frameId);
            }
            streamStatsPartialPruned += stale.Count;

            if (partialStreamFrames.Count > 24)
            {
                var overflow = partialStreamFrames
                    .OrderByDescending(pair => pair.Value.LastUpdatedUtc)
                    .Skip(24)
                    .Select(pair => pair.Key)
                    .ToList();
                foreach (var frameId in overflow)
                {
                    partialStreamFrames.Remove(frameId);
                }
                streamStatsPartialPruned += overflow.Count;
            }
        }

        private bool MarkNewestObservedFrameId(uint frameId)
        {
            if (hasNewestObservedFrameId && !IsFrameIdNewer(frameId, newestObservedFrameId))
            {
                return false;
            }

            newestObservedFrameId = frameId;
            hasNewestObservedFrameId = true;
            return true;
        }

        private bool IsBehindNewestObservedFrameId(uint frameId)
        {
            return hasNewestObservedFrameId && IsFrameIdNewer(newestObservedFrameId, frameId);
        }

        private bool IsObsoleteFrameId(uint frameId)
        {
            return hasCompletedStreamFrame && !IsFrameIdNewer(frameId, latestCompletedFrameId);
        }

        private void RecordCompletedStreamFrame(uint frameId)
        {
            if (streamStatsHasCompletedFrameId && IsFrameIdNewer(frameId, streamStatsLastCompletedFrameId))
            {
                var delta = unchecked((int)(frameId - streamStatsLastCompletedFrameId));
                if (delta > 1)
                {
                    streamStatsFrameGaps += delta - 1;
                }
            }

            streamStatsFrames++;
            streamStatsLastCompletedFrameId = frameId;
            streamStatsHasCompletedFrameId = true;
        }

        private void LogStreamReceiveStats(string reason, bool force = false)
        {
            if (streamStatsSinceUtc == default(DateTime))
            {
                return;
            }

            var now = DateTime.UtcNow;
            if (!force && now < nextStreamStatsLogUtc)
            {
                return;
            }

            var elapsed = Math.Max(0.001, (now - streamStatsSinceUtc).TotalSeconds);
            RemoteExplorerDiagnostics.Info(
                "UDP/JPEG receive stats " +
                $"reason={reason} elapsed={elapsed:0.0}s packets={streamStatsPackets} bytes={streamStatsBytes} " +
                $"frames={streamStatsFrames} fps={(streamStatsFrames / elapsed):0.0} frame_gaps={streamStatsFrameGaps} " +
                $"partials={partialStreamFrames.Count} partials_created={streamStatsPartialsCreated} partials_pruned={streamStatsPartialPruned} " +
                $"invalid_packets={streamStatsInvalidPackets} invalid_chunks={streamStatsInvalidChunks} " +
                $"generation_drops={streamStatsGenerationDrops} behind_packets={streamStatsBehindPackets} " +
                $"obsolete_packets={streamStatsObsoletePackets} mismatched_frames={streamStatsMismatchedFrames} " +
                $"chunk_add_failures={streamStatsChunkAddFailures} last_frame={streamStatsLastCompletedFrameId}");

            streamStatsSinceUtc = now;
            nextStreamStatsLogUtc = now.AddSeconds(StreamStatsIntervalSeconds);
            streamStatsPackets = 0;
            streamStatsBytes = 0;
            streamStatsFrames = 0;
            streamStatsFrameGaps = 0;
            streamStatsPartialsCreated = 0;
            streamStatsPartialPruned = 0;
            streamStatsInvalidPackets = 0;
            streamStatsInvalidChunks = 0;
            streamStatsGenerationDrops = 0;
            streamStatsBehindPackets = 0;
            streamStatsObsoletePackets = 0;
            streamStatsMismatchedFrames = 0;
            streamStatsChunkAddFailures = 0;
        }

        // Frame ids are unsigned on the wire; signed delta preserves order across wraparound.
        private static bool IsFrameIdNewer(uint candidate, uint baseline)
        {
            return candidate != baseline && unchecked((int)(candidate - baseline)) > 0;
        }

        private static void ConfigureStreamSocket(Socket socket)
        {
            if (socket == null)
            {
                return;
            }

            try
            {
                // 大缓冲吸收高分辨率 JPEG 的 UDP 分片突发。 / Larger buffer absorbs high-resolution JPEG chunk bursts.
                socket.ReceiveBufferSize = StreamReceiveBufferBytes;
                socket.IOControl((IOControlCode)SioUdpConnectionReset, new byte[] { 0, 0, 0, 0 }, null);
            }
            catch (SocketException)
            {
            }
            catch (ObjectDisposedException)
            {
            }
            catch (NotSupportedException)
            {
            }
        }

        private async Task SendStreamFirewallPunchesAsync(int serverSourcePort, CancellationToken cancellationToken)
        {
            if (streamClient == null || controlEndpoint == null || serverSourcePort <= 0 || serverSourcePort > 65535)
            {
                RemoteExplorerDiagnostics.Info("UDP/JPEG stream firewall punch skipped; server source port unavailable.");
                return;
            }

            var endpoint = new IPEndPoint(controlEndpoint.Address, serverSourcePort);
            var probe = Encoding.ASCII.GetBytes("REXPPING");
            for (var i = 0; i < 3; i++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                try
                {
                    await streamClient.SendAsync(probe, probe.Length, endpoint);
                    RemoteExplorerDiagnostics.Info($"UDP/JPEG stream firewall punch sent to {endpoint} attempt={i + 1}");
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (SocketException ex)
                {
                    RemoteExplorerDiagnostics.Info("UDP/JPEG stream firewall punch failed: " + ex.Message);
                    return;
                }

                if (i < 2)
                {
                    await Task.Delay(20, cancellationToken);
                }
            }
        }

        private static string ResolveLocalAddressFor(IPEndPoint remoteEndpoint)
        {
            if (remoteEndpoint == null)
            {
                return null;
            }

            try
            {
                using (var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp))
                {
                    socket.Connect(remoteEndpoint);
                    var localEndpoint = socket.LocalEndPoint as IPEndPoint;
                    var address = localEndpoint != null ? localEndpoint.Address : null;
                    if (address != null && !IPAddress.Any.Equals(address))
                    {
                        return address.ToString();
                    }
                }
            }
            catch (SocketException)
            {
            }
            catch (ObjectDisposedException)
            {
            }

            return null;
        }

        private static ushort ReadUInt16(byte[] bytes, int offset)
        {
            return (ushort)((bytes[offset] << 8) | bytes[offset + 1]);
        }

        private static uint ReadUInt32(byte[] bytes, int offset)
        {
            return
                ((uint)bytes[offset] << 24) |
                ((uint)bytes[offset + 1] << 16) |
                ((uint)bytes[offset + 2] << 8) |
                bytes[offset + 3];
        }

        private static int ClampStreamFps(int fps)
        {
            return Math.Max(MinStreamFps, Math.Min(MaxStreamFps, fps));
        }

        private async Task<T> SendRequestAsync<T>(
            Dictionary<string, object> message,
            CancellationToken cancellationToken,
            float timeoutSeconds = DefaultControlTimeoutSeconds)
            where T : class
        {
            if (controlClient == null || controlEndpoint == null)
            {
                throw new InvalidOperationException("Control socket is not ready.");
            }

            await requestLock.WaitAsync(cancellationToken);
            try
            {
                var requestId = Convert.ToString(message["request_id"]);
                var payload = RemoteExplorerProtocol.Encode(message);
                await controlClient.SendAsync(payload, payload.Length, controlEndpoint);

                var deadline = DateTime.UtcNow.AddSeconds(Math.Max(0.5f, timeoutSeconds));
                while (DateTime.UtcNow < deadline)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    var remaining = deadline - DateTime.UtcNow;
                    var receiveTask = controlClient.ReceiveAsync();
                    var completed = await Task.WhenAny(receiveTask, Task.Delay(remaining, cancellationToken));
                    if (completed != receiveTask)
                    {
                        break;
                    }

                    var received = await receiveTask;
                    var json = Encoding.UTF8.GetString(received.Buffer);
                    var baseEnvelope = JsonUtility.FromJson<CommandEnvelope>(json);
                    if (baseEnvelope == null || baseEnvelope.request_id != requestId)
                    {
                        continue;
                    }

                    var parsed = JsonUtility.FromJson<T>(json);
                    if (parsed == null)
                    {
                        throw new InvalidOperationException("Could not parse server response.");
                    }

                    if (baseEnvelope.type == "error")
                    {
                        if (typeof(T) == typeof(CommandEnvelope))
                        {
                            return parsed;
                        }

                        var messageText = baseEnvelope.error != null
                            ? $"{baseEnvelope.error.code}: {baseEnvelope.error.message}"
                            : "Server returned an error.";
                        throw new InvalidOperationException(messageText);
                    }

                    return parsed;
                }

                throw new TimeoutException(
                    $"Timed out waiting for server response after {Math.Max(0.5f, timeoutSeconds):0.#}s.");
            }
            finally
            {
                requestLock.Release();
            }
        }
    }
}
