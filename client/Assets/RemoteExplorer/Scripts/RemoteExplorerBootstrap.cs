using UnityEngine;

namespace RemoteExplorer
{
    public static class RemoteExplorerBootstrap
    {
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        private static void BootBeforeSceneLoad()
        {
            Boot();
        }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void BootAfterSceneLoad()
        {
            Boot();
        }

        private static void Boot()
        {
            if (Object.FindObjectOfType<RemoteExplorerApp>() != null)
            {
                return;
            }

            var app = new GameObject("Remote Explorer Client");
            Object.DontDestroyOnLoad(app);
            app.AddComponent<RemoteExplorerApp>();
        }
    }
}
