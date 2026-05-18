using UnityEngine;

namespace RemoteExplorer
{
    public static class RemoteExplorerBootstrap
    {
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void BootAfterSceneLoad()
        {
            Boot();
        }

        private static void Boot()
        {
            if (Object.FindObjectOfType<RemoteExplorerApp>(true) != null)
            {
                return;
            }

            var app = new GameObject("Remote Explorer Client");
            Object.DontDestroyOnLoad(app);
            app.AddComponent<RemoteExplorerApp>();
        }
    }
}
