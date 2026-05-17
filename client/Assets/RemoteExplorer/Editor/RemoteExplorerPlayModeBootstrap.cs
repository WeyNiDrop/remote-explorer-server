#if UNITY_EDITOR
using RemoteExplorer;
using UnityEditor;
using UnityEngine;

namespace RemoteExplorer.Editor
{
    [InitializeOnLoad]
    public static class RemoteExplorerPlayModeBootstrap
    {
        static RemoteExplorerPlayModeBootstrap()
        {
            EditorApplication.playModeStateChanged -= OnPlayModeStateChanged;
            EditorApplication.playModeStateChanged += OnPlayModeStateChanged;
        }

        private static void OnPlayModeStateChanged(PlayModeStateChange state)
        {
            if (state != PlayModeStateChange.ExitingEditMode)
            {
                return;
            }

            EnsureRuntimeEntry();
        }

        [MenuItem("Remote Explorer/Ensure Runtime Entry In Current Scene")]
        public static void EnsureRuntimeEntry()
        {
            var existing = Object.FindObjectOfType<RemoteExplorerApp>(true);
            if (existing != null)
            {
                Debug.Log("[RemoteExplorer] Runtime entry already exists in current scene.");
                return;
            }

            var entry = new GameObject("Remote Explorer Client");
            entry.AddComponent<RemoteExplorerApp>();
            Debug.Log("[RemoteExplorer] Runtime entry added to current scene for Play Mode.");
        }
    }
}
#endif
