using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
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

        private static readonly byte[] StreamMagic = Encoding.ASCII.GetBytes("REXPSTR1");
        private const int StreamHeaderBytes = 24;
        private const int StreamChunkBytes = 1000;
        private const int MaxStreamChunks = 512;
        private const int StreamReceiveBufferBytes = 384 * 1024;
        private const int MaxReceiveBurstPackets = 256;
        private const int SioUdpConnectionReset = -1744830452;
        public const int DefaultStreamFps = 30;
        public const int MinStreamFps = 20;
        public const int MaxStreamFps = 60;
        public const int DefaultStreamQuality = 55;

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
            var requestId = RemoteExplorerProtocol.NewRequestId();
            var discover = new Dictionary<string, object>
            {
                ["v"] = RemoteExplorerProtocol.Version,
                ["type"] = "discover",
                ["request_id"] = requestId,
                ["client"] = new Dictionary<string, object>
                {
                    ["id"] = RemoteExplorerProtocol.ClientId(),
                    ["name"] = RemoteExplorerProtocol.ClientName()
                }
            };

            using (var udp = new UdpClient(0))
            {
                udp.EnableBroadcast = true;
                var payload = RemoteExplorerProtocol.Encode(discover);
                foreach (var endpoint in BroadcastEndpoints(discoveryPort))
                {
                    await udp.SendAsync(payload, payload.Length, endpoint);
                }

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
                        received = receiveTask.Result;
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

        private static IEnumerable<IPEndPoint> BroadcastEndpoints(int discoveryPort)
        {
            var sent = new HashSet<string>();
            var fallback = new IPEndPoint(IPAddress.Broadcast, discoveryPort);
            sent.Add(fallback.ToString());
            yield return fallback;

            var loopback = new IPEndPoint(IPAddress.Loopback, discoveryPort);
            sent.Add(loopback.ToString());
            yield return loopback;

            NetworkInterface[] interfaces;
            try
            {
                interfaces = NetworkInterface.GetAllNetworkInterfaces();
            }
            catch
            {
                yield break;
            }

            foreach (var networkInterface in interfaces)
            {
                if (networkInterface.OperationalStatus != OperationalStatus.Up)
                {
                    continue;
                }

                IPInterfaceProperties properties;
                try
                {
                    properties = networkInterface.GetIPProperties();
                }
                catch
                {
                    continue;
                }

                foreach (var unicast in properties.UnicastAddresses)
                {
                    if (unicast.Address.AddressFamily != AddressFamily.InterNetwork || unicast.IPv4Mask == null)
                    {
                        continue;
                    }

                    var broadcast = CalculateBroadcast(unicast.Address, unicast.IPv4Mask);
                    if (broadcast == null)
                    {
                        continue;
                    }

                    var endpoint = new IPEndPoint(broadcast, discoveryPort);
                    if (sent.Add(endpoint.ToString()))
                    {
                        yield return endpoint;
                    }
                }
            }
        }

        private static IPAddress CalculateBroadcast(IPAddress address, IPAddress mask)
        {
            var addressBytes = address.GetAddressBytes();
            var maskBytes = mask.GetAddressBytes();
            if (addressBytes.Length != 4 || maskBytes.Length != 4)
            {
                return null;
            }

            var broadcastBytes = new byte[4];
            for (var i = 0; i < 4; i++)
            {
                broadcastBytes[i] = (byte)(addressBytes[i] | ~maskBytes[i]);
            }

            return new IPAddress(broadcastBytes);
        }

        public async Task ConnectAsync(DiscoveredServer server, string password, CancellationToken cancellationToken = default)
        {
            Disconnect();
            controlEndpoint = new IPEndPoint(IPAddress.Parse(server.Address), server.ControlPort);
            controlClient = new UdpClient(0);
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
            streamCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            var localStreamGeneration = BeginLocalStreamState();
            streamReceiveTask = ReceiveStreamLoopAsync(streamClient, streamCancellation.Token, localStreamGeneration);

            var result = await SendCommandAsync(
                "stream_start",
                new Dictionary<string, object>
                {
                    ["port"] = streamPort,
                    ["resolution"] = resolution,
                    ["fps"] = ClampStreamFps(fps),
                    ["quality"] = quality
                },
                cancellationToken);

            if (!result.ok && result.type != "result")
            {
                StopLocalStream();
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

        public async Task<CommandEnvelope> BrowserCommandAsync(string command, CancellationToken cancellationToken = default)
        {
            return await SendCommandAsync(command, new Dictionary<string, object>(), cancellationToken);
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
                cancellationToken);
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
                cancellationToken);
        }

        public async Task<CommandEnvelope> SendCommandAsync(
            string command,
            Dictionary<string, object> payload,
            CancellationToken cancellationToken = default)
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

            return await SendRequestAsync<CommandEnvelope>(message, cancellationToken);
        }

        public void Disconnect()
        {
            StopLocalStream();
            sessionId = null;
            sessionKey = null;
            commandCounter = 0;
            ConnectedServer = null;
            controlEndpoint = null;
            controlClient?.Close();
            controlClient?.Dispose();
            controlClient = null;
        }

        public void Dispose()
        {
            Disconnect();
            requestLock.Dispose();
        }

        private void StopLocalStream()
        {
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
            if (packet == null || packet.Length <= StreamHeaderBytes)
            {
                return;
            }

            for (var i = 0; i < StreamMagic.Length; i++)
            {
                if (packet[i] != StreamMagic[i])
                {
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
                return;
            }

            lock (streamLock)
            {
                if (generation != streamGeneration)
                {
                    return;
                }

                if (MarkNewestObservedFrameId(frameId))
                {
                    RemovePartialFramesOlderThan(frameId);
                }
                else if (IsBehindNewestObservedFrameId(frameId))
                {
                    return;
                }

                if (IsObsoleteFrameId(frameId))
                {
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
                }

                if (builder.ChunkCount != chunkCount ||
                    builder.Width != width ||
                    builder.Height != height ||
                    builder.SourceWidth != sourceWidth ||
                    builder.SourceHeight != sourceHeight)
                {
                    partialStreamFrames.Remove(frameId);
                    return;
                }

                if (builder.AddChunk(
                        chunkIndex,
                        packet,
                        StreamHeaderBytes,
                        packet.Length - StreamHeaderBytes))
                {
                    var completedFrame = builder.Build();
                    partialStreamFrames.Remove(frameId);

                    if (!IsObsoleteFrameId(completedFrame.FrameId))
                    {
                        latestCompletedStreamFrame = completedFrame;
                        latestCompletedFrameId = completedFrame.FrameId;
                        hasCompletedStreamFrame = true;
                    }
                }

                PrunePartialFrames();
            }
        }

        private void PrunePartialFrames()
        {
            var cutoff = DateTime.UtcNow.AddSeconds(-2);
            var stale = partialStreamFrames
                .Where(pair =>
                    pair.Value.LastUpdatedUtc < cutoff ||
                    IsObsoleteFrameId(pair.Key) ||
                    IsBehindNewestObservedFrameId(pair.Key))
                .Select(pair => pair.Key)
                .ToList();
            foreach (var frameId in stale)
            {
                partialStreamFrames.Remove(frameId);
            }

            if (partialStreamFrames.Count > 10)
            {
                var overflow = partialStreamFrames
                    .OrderByDescending(pair => pair.Value.LastUpdatedUtc)
                    .Skip(10)
                    .Select(pair => pair.Key)
                    .ToList();
                foreach (var frameId in overflow)
                {
                    partialStreamFrames.Remove(frameId);
                }
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

        private void RemovePartialFramesOlderThan(uint frameId)
        {
            var stale = partialStreamFrames
                .Where(pair => IsFrameIdNewer(frameId, pair.Key))
                .Select(pair => pair.Key)
                .ToList();
            foreach (var staleFrameId in stale)
            {
                partialStreamFrames.Remove(staleFrameId);
            }
        }

        private bool IsObsoleteFrameId(uint frameId)
        {
            return hasCompletedStreamFrame && !IsFrameIdNewer(frameId, latestCompletedFrameId);
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
            CancellationToken cancellationToken)
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

                var deadline = DateTime.UtcNow.AddSeconds(3);
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

                    var received = receiveTask.Result;
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

                throw new TimeoutException("Timed out waiting for server response.");
            }
            finally
            {
                requestLock.Release();
            }
        }
    }
}
