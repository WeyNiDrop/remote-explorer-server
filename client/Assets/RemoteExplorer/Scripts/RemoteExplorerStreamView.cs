using System;
using UnityEngine;
using UnityEngine.EventSystems;

namespace RemoteExplorer
{
    public class RemoteExplorerStreamView : MonoBehaviour, IPointerDownHandler, IPointerUpHandler, IDragHandler
    {
        private const float TapMaxSeconds = 0.45f;
        private const float SwipeThresholdNormalized = 0.035f;

        public Action<Vector2> OnTap;
        public Action<Vector2> OnSwipe;

        private Vector2 pointerDownNormalized;
        private Vector2 lastNormalized;
        private float lastPointerDownTime = -1f;
        private int activePointerId;
        private bool hasPointer;
        private bool movedEnoughForSwipe;

        public void OnPointerDown(PointerEventData eventData)
        {
            Vector2 normalized;
            if (!TryGetNormalizedPoint(eventData, out normalized))
            {
                hasPointer = false;
                return;
            }

            activePointerId = eventData.pointerId;
            hasPointer = true;
            movedEnoughForSwipe = false;
            pointerDownNormalized = normalized;
            lastNormalized = normalized;
            lastPointerDownTime = Time.unscaledTime;
            RemoteExplorerDiagnostics.Info($"Preview pointer down normalized=({normalized.x:F3},{normalized.y:F3})");
        }

        public void OnDrag(PointerEventData eventData)
        {
            if (!IsActivePointer(eventData))
            {
                return;
            }

            Vector2 normalized;
            if (!TryGetNormalizedPoint(eventData, out normalized))
            {
                return;
            }

            lastNormalized = normalized;
            if ((lastNormalized - pointerDownNormalized).magnitude >= SwipeThresholdNormalized)
            {
                movedEnoughForSwipe = true;
            }
        }

        public void OnPointerUp(PointerEventData eventData)
        {
            if (!IsActivePointer(eventData))
            {
                return;
            }

            Vector2 normalized;
            if (TryGetNormalizedPoint(eventData, out normalized))
            {
                lastNormalized = normalized;
            }

            var delta = lastNormalized - pointerDownNormalized;
            hasPointer = false;

            if (movedEnoughForSwipe || delta.magnitude >= SwipeThresholdNormalized)
            {
                RemoteExplorerDiagnostics.Info(
                    $"Preview swipe delta=({delta.x:F3},{delta.y:F3}) start=({pointerDownNormalized.x:F3},{pointerDownNormalized.y:F3})");
                OnSwipe?.Invoke(delta);
                return;
            }

            if (Time.unscaledTime - lastPointerDownTime <= TapMaxSeconds)
            {
                RemoteExplorerDiagnostics.Info($"Preview tap normalized=({lastNormalized.x:F3},{lastNormalized.y:F3})");
                OnTap?.Invoke(lastNormalized);
            }
        }

        private bool IsActivePointer(PointerEventData eventData)
        {
            return hasPointer && eventData.pointerId == activePointerId;
        }

        private bool TryGetNormalizedPoint(PointerEventData eventData, out Vector2 normalized)
        {
            normalized = default(Vector2);
            var rect = transform as RectTransform;
            if (rect == null)
            {
                return false;
            }

            Vector2 localPoint;
            if (!RectTransformUtility.ScreenPointToLocalPointInRectangle(
                    rect,
                    eventData.position,
                    eventData.pressEventCamera,
                    out localPoint))
            {
                return false;
            }

            normalized = new Vector2(
                Mathf.InverseLerp(rect.rect.xMin, rect.rect.xMax, localPoint.x),
                Mathf.InverseLerp(rect.rect.yMin, rect.rect.yMax, localPoint.y));

            if (normalized.x < 0f || normalized.x > 1f || normalized.y < 0f || normalized.y > 1f)
            {
                return false;
            }

            return true;
        }
    }
}
