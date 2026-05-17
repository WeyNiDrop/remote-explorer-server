using System;
using UnityEngine;
using UnityEngine.EventSystems;

namespace RemoteExplorer
{
    public class RemoteExplorerStreamView : MonoBehaviour, IPointerDownHandler, IPointerClickHandler
    {
        public Action<Vector2> OnTap;
        private float lastPointerDownTime = -1f;

        public void OnPointerDown(PointerEventData eventData)
        {
            lastPointerDownTime = Time.unscaledTime;
            HandlePointer(eventData);
        }

        public void OnPointerClick(PointerEventData eventData)
        {
            if (Time.unscaledTime - lastPointerDownTime < 0.35f)
            {
                return;
            }

            HandlePointer(eventData);
        }

        private void HandlePointer(PointerEventData eventData)
        {
            var rect = transform as RectTransform;
            if (rect == null)
            {
                return;
            }

            Vector2 localPoint;
            if (!RectTransformUtility.ScreenPointToLocalPointInRectangle(
                    rect,
                    eventData.position,
                    eventData.pressEventCamera,
                    out localPoint))
            {
                return;
            }

            var normalized = new Vector2(
                Mathf.InverseLerp(rect.rect.xMin, rect.rect.xMax, localPoint.x),
                Mathf.InverseLerp(rect.rect.yMin, rect.rect.yMax, localPoint.y));

            if (normalized.x < 0f || normalized.x > 1f || normalized.y < 0f || normalized.y > 1f)
            {
                return;
            }

            Debug.Log($"[RemoteExplorer] Preview pointer normalized=({normalized.x:F3},{normalized.y:F3})");
            OnTap?.Invoke(normalized);
        }
    }
}
