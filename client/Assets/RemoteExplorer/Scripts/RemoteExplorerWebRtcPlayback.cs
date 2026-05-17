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
        private RemoteExplorerClient client;
        private RawImage targetImage;
        private Action<string> statusSink;
        private RTCPeerConnection peerConnection;
        private string peerId;
        private bool initialized;
        private Coroutine updateCoroutine;

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

            yield return StopRoutine(cancellationToken, null);
            EnsureInitialized();

            peerConnection = new RTCPeerConnection();
            peerConnection.OnTrack = OnTrack;
            peerConnection.OnConnectionStateChange = state =>
            {
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
                completion.TrySetException(new InvalidOperationException(offerOperation.Error.message));
                yield break;
            }

            var offer = offerOperation.Desc;
            var localOperation = peerConnection.SetLocalDescription(ref offer);
            yield return localOperation;
            if (localOperation.IsError)
            {
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
                completion.TrySetException(offerTask.Exception);
                yield break;
            }

            var result = offerTask.Result;
            if (!result.ok && result.type != "result")
            {
                completion.TrySetResult(result);
                yield break;
            }

            var answer = result.result;
            if (answer == null || string.IsNullOrEmpty(answer.sdp))
            {
                completion.TrySetException(new InvalidOperationException("Server did not return a WebRTC answer."));
                yield break;
            }

            peerId = answer.peer_id ?? string.Empty;
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
                completion.TrySetException(new InvalidOperationException(remoteOperation.Error.message));
                yield break;
            }

            completion.TrySetResult(result);
        }

        private IEnumerator StopRoutine(
            CancellationToken cancellationToken,
            TaskCompletionSource<bool> completion)
        {
            var stoppedPeerId = peerId;
            peerId = null;

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
            if (initialized)
            {
                return;
            }

            WebRTC.Initialize();
            updateCoroutine = StartCoroutine(WebRTC.Update());
            initialized = true;
        }

        private void OnTrack(RTCTrackEvent trackEvent)
        {
            var videoTrack = trackEvent.Track as VideoStreamTrack;
            if (videoTrack == null)
            {
                return;
            }

            videoTrack.OnVideoReceived += texture =>
            {
                if (targetImage == null || texture == null)
                {
                    return;
                }

                targetImage.texture = texture;
                targetImage.color = Color.white;
                OnFrameSize?.Invoke(Mathf.Max(1, texture.width), Mathf.Max(1, texture.height));
                OnFrameReceived?.Invoke();
            };
        }

        private void OnDestroy()
        {
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

            if (initialized)
            {
                WebRTC.Dispose();
                initialized = false;
            }
        }
    }
}
#endif
