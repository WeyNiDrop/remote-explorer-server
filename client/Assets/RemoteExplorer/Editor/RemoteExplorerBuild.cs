#if UNITY_EDITOR
using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace RemoteExplorer.Editor
{
    public static class RemoteExplorerBuild
    {
        private const string BuildRoot = "Builds";
        private const string GeneratedScenePath = "Assets/RemoteExplorer/GeneratedRemoteExplorerScene.unity";

        [MenuItem("Remote Explorer/Build Android Development")]
        public static void BuildAndroidDevelopment()
        {
            EnsureBuildScene();
            BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = BuildScenes(),
                locationPathName = Path.Combine(BuildRoot, "Android", "RemoteExplorer.apk"),
                target = BuildTarget.Android,
                options = BuildOptions.Development
            });
        }

        [MenuItem("Remote Explorer/Build iOS Development")]
        public static void BuildIosDevelopment()
        {
            EnsureBuildScene();
            BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = BuildScenes(),
                locationPathName = Path.Combine(BuildRoot, "iOS"),
                target = BuildTarget.iOS,
                options = BuildOptions.Development
            });
        }

        [MenuItem("Remote Explorer/Ensure Build Scene")]
        public static void EnsureBuildScene()
        {
            Directory.CreateDirectory(Path.GetDirectoryName(GeneratedScenePath) ?? "Assets/RemoteExplorer");

            if (File.Exists(GeneratedScenePath))
            {
                EnsureSceneInBuildSettings();
                return;
            }

            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            scene.name = "RemoteExplorer";
            EditorSceneManager.SaveScene(scene, GeneratedScenePath);
            EnsureSceneInBuildSettings();
        }

        private static string[] BuildScenes()
        {
            EnsureSceneInBuildSettings();
            return new[] { GeneratedScenePath };
        }

        private static void EnsureSceneInBuildSettings()
        {
            var scene = new EditorBuildSettingsScene(GeneratedScenePath, true);
            EditorBuildSettings.scenes = new[] { scene };
        }
    }
}
#endif
