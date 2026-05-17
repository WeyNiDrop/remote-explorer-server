#if REMOTE_EXPLORER_HAS_WEBRTC
using System;
using System.Collections;
using System.Threading;
using System.Threading.Tasks;
using Unity.WebRTC;
using UnityEngine;
using UnityEngine.UI;

namespace RemoteExplorer
{
    public class RemoteExplorerWebRtcPlayback : MonoBehaviour
    {
        private const float WebRtcStatsIntervalSeconds = 5f;

        private RemoteExplorerClient client;
        private RawImage targetImage;
        private Action<string> statusSink;
        private RTCPeerConnection peerConnection;
        private string peerId;
        private Coroutine updateCoroutine;
        private float statsSince = -1f;
        private float nextStatsLogAt = -1f;
        private int receivedFrames;
        private int lastTextureWidth;
        private int lastTextureHeight;

        public bool IsActive => peerConnection != null && !string.IsNullOrEmpty(peerId);
        public Action<int, int> OnFrameSize;
        public Action OnFrameReceived;

        public void Configure(
            RemoteExplorerClient remoteClient,
            RawImage image,
            Action<string> setStatus)
        {
            client = remoteClient;
            targetImage = image;
            statusSink = setStatus;
        }

        public Task<CommandEnvelope> StartAsync(
            string resolution,
            int fps,
            CancellationToken cancellationToken)
        {
            var completion = new TaskCompletionSource<CommandEnvelope>();
            StartCoroutine(StartRoutine(resolution, fps, cancellationToken, completion));
            return completion.Task;
        }

        public Task StopAsync(CancellationToken cancellationToken)
        {
            var completion = new TaskCompletionSource<bool>();
            StartCoroutine(StopRoutine(cancellationToken, completion));
            return completion.Task;
        }

        private IEnumerator StartRoutine(
            string resolution,
            int fps,
            CancellationToken cancellationToken,
            TaskCompletionSource<CommandEnvelope> completion)
        {
            if (client == null)
            {
                completion.TrySetException(new InvalidOperationException("WebRTC playback is not configured."));
                yield break;
            }

            RemoteExplorerDiagnostics.Info($"WebRTC client start resolution={resolution} fps={fps}");
            yield return StopRoutine(cancellationToken, null);
            ResetStats();
            EnsureInitialized();

            peerConnection = new RTCPeerConnection();
            peerConnection.OnTrack = OnTrack;
            peerConnection.OnConnectionStateChange = state =>
            {
                RemoteExplorerDiagnostics.Info("WebRTC connection state=" + state);
                if (state == RTCPeerConnectionState.Failed ||
                    state == RTCPeerConnectionState.Closed ||
                    state == RTCPeerConnectionState.Disconnected)
                {
                    statusSink?.Invoke("WebRTC disconnected");
                }
            };

            var transceiver = peerConnection.AddTransceiver(TrackKind.Video);
            transceiver.Direction = RTCRtpTransceiverDirection.RecvOnly;

            var offerOperation = peerConnection.CreateOffer();
            yield return offerOperation;
            if (offerOperation.IsError)
            {
                Debug.LogWarning("[RemoteExplorer] WebRTC CreateOffer failed: " + offerOperation.Error.message);
                completion.TrySetException(new InvalidOperationException(offerOperation.Error.message));
                yield break;
            }

            var offer = offerOperation.Desc;
            RemoteExplorerDiagnostics.Info("WebRTC local offer created sdp_bytes=" + (offer.sdp != null ? offer.sdp.Length : 0));
            var localOperation = peerConnection.SetLocalDescription(ref offer);
            yield return localOperation;
            if (localOperation.IsError)
            {
                Debug.LogWarning("[RemoteExplorer] WebRTC SetLocalDescription failed: " + localOperation.Error.message);
                completion.TrySetException(new InvalidOperationException(localOperation.Error.message));
                yield break;
            }

            Task<CommandEnvelope> offerTask;
            try
            {
                offerTask = client.WebRtcOfferAsync(offer.sdp, "offer", resolution, fps, cancellationToken);
            }
            catch (Exception ex)
            {
                completion.TrySetException(ex);
                yield break;
            }

            while (!offerTask.IsCompleted)
            {
                if (cancellationToken.IsCancellationRequested)
                {
                    completion.TrySetCanceled();
                    yield break;
                }

                yield return null;
            }

            if (offerTask.IsFaulted)
            {
                Debug.LogWarning("[RemoteExplorer] WebRTC offer command failed: " + UnwrapTaskException(offerTask.Exception).Message);
                completion.TrySetException(UnwrapTaskException(offerTask.Exception));
                yield break;
            }

            var result = offerTask.Result;
            if (!result.ok && result.type != "result")
            {
                var error = result.error != null ? $"{result.error.code}: {result.error.message}" : "Unknown error";
                Debug.LogWarning("[RemoteExplorer] WebRTC offer rejected: " + error);
                completion.TrySetResult(result);
                yield break;
            }

            var answer = result.result;
            if (answer == null || string.IsNullOrEmpty(answer.sdp))
            {
                Debug.LogWarning("[RemoteExplorer] WebRTC answer missing SDP.");
                completion.TrySetException(new InvalidOperationException("Server did not return a WebRTC answer."));
                yield break;
            }

            peerId = answer.peer_id ?? string.Empty;
            RemoteExplorerDiagnostics.Info(
                $"WebRTC answer received peer_id={peerId} type={answer.type} codec={answer.codec} size={answer.width}x{answer.height} fps={answer.fps} sdp_bytes={answer.sdp.Length}");
            OnFrameSize?.Invoke(Mathf.Max(1, answer.width), Mathf.Max(1, answer.height));

            var answerDescription = new RTCSessionDescription
            {
                type = RTCSdpType.Answer,
                sdp = answer.sdp
            };
            var remoteOperation = peerConnection.SetRemoteDescription(ref answerDescription);
            yield return remoteOperation;
            if (remoteOperation.IsError)
            {
                Debug.LogWarning("[RemoteExplorer] WebRTC SetRemoteDescription failed: " + remoteOperation.Error.message);
                completion.TrySetException(new InvalidOperationException(remoteOperation.Error.message));
                yield break;
            }

            RemoteExplorerDiagnostics.Info("WebRTC remote description applied.");
            completion.TrySetResult(result);
        }

        private IEnumerator StopRoutine(
            CancellationToken cancellationToken,
            TaskCompletionSource<bool> completion)
        {
            var stoppedPeerId = peerId;
            peerId = null;
            if (!string.IsNullOrEmpty(stoppedPeerId))
            {
                RemoteExplorerDiagnostics.Info("WebRTC client stop peer_id=" + stoppedPeerId);
                LogStats("stop", true);
            }

            if (peerConnection != null)
            {
                peerConnection.Close();
                peerConnection.Dispose();
                peerConnection = null;
            }

            if (client != null && !string.IsNullOrEmpty(stoppedPeerId) && client.IsConnected)
            {
                var stopTask = client.StopWebRtcAsync(stoppedPeerId, cancellationToken);
                while (!stopTask.IsCompleted)
                {
                    if (cancellationToken.IsCancellationRequested)
                    {
                        Debug.LogWarning("[RemoteExplorer] WebRTC stop cancelled peer_id=" + stoppedPeerId);
                        completion?.TrySetCanceled();
                        yield break;
                    }

                    yield return null;
                }
            }

            completion?.TrySetResult(true);
        }

        private void EnsureInitialized()
        {
            if (updateCoroutine != null)
            {
                return;
            }

            updateCoroutine = StartCoroutine(WebRTC.Update());
            RemoteExplorerDiagnostics.Info("WebRTC update coroutine started.");
        }

        private static Exception UnwrapTaskException(Exception exception)
        {
            var aggregate = exception as AggregateException;
            if (aggregate == null)
            {
                return exception;
            }

            var flattened = aggregate.Flatten();
            return flattened.InnerExceptions.Count == 1
                ? flattened.InnerExceptions[0]
                : flattened.GetBaseException();
        }

        private void OnTrack(RTCTrackEvent trackEvent)
        {
            var videoTrack = trackEvent.Track as VideoStreamTrack;
            if (videoTrack == null)
            {
                Debug.LogWarning("[RemoteExplorer] WebRTC track event did not contain a video track.");
                return;
            }

            RemoteExplorerDiagnostics.Info("WebRTC video track received.");
            // 这个回调只保证接收 texture 已创建，不代表每帧到达。
            // This callback means the receive texture exists; it is not a per-frame callback.
            videoTrack.OnVideoReceived += texture =>
            {
                if (targetImage == null || texture == null)
                {
                    return;
                }

                targetImage.texture = texture;
                targetImage.color = Color.white;
                RecordFrame(texture.width, texture.height);
                OnFrameSize?.Invoke(Mathf.Max(1, texture.width), Mathf.Max(1, texture.height));
                OnFrameReceived?.Invoke();
            };
        }

        private void ResetStats()
        {
            statsSince = Time.unscaledTime;
            nextStatsLogAt = Time.unscaledTime + WebRtcStatsIntervalSeconds;
            receivedFrames = 0;
            lastTextureWidth = 0;
            lastTextureHeight = 0;
        }

        private void RecordFrame(int width, int height)
        {
            if (statsSince < 0f)
            {
                ResetStats();
            }

            receivedFrames++;
            lastTextureWidth = Mathf.Max(1, width);
            lastTextureHeight = Mathf.Max(1, height);
            LogStats("interval");
        }

        private void LogStats(string reason, bool force = false)
        {
            if (statsSince < 0f)
            {
                return;
            }

            if (!force && Time.unscaledTime < nextStatsLogAt)
            {
                return;
            }

            var elapsed = Mathf.Max(0.001f, Time.unscaledTime - statsSince);
            RemoteExplorerDiagnostics.Info(
                "WebRTC render stats " +
                $"reason={reason} elapsed={elapsed:0.0}s texture_callbacks={receivedFrames} callback_rate={(receivedFrames / elapsed):0.0} " +
                $"last_texture={lastTextureWidth}x{lastTextureHeight} peer_id={(string.IsNullOrEmpty(peerId) ? "none" : peerId)}");

            statsSince = Time.unscaledTime;
            nextStatsLogAt = Time.unscaledTime + WebRtcStatsIntervalSeconds;
            receivedFrames = 0;
        }

        private void OnDestroy()
        {
            LogStats("destroy", true);
            if (peerConnection != null)
            {
                peerConnection.Close();
                peerConnection.Dispose();
                peerConnection = null;
            }

            if (updateCoroutine != null)
            {
                StopCoroutine(updateCoroutine);
                updateCoroutine = null;
            }

        }
    }
}
#endif
