#if UNITY_EDITOR && UNITY_IOS
using System.IO;
using UnityEditor;
using UnityEditor.Callbacks;
using UnityEditor.iOS.Xcode;

namespace RemoteExplorer.Editor
{
    public static class RemoteExplorerIosPostprocess
    {
        private const string EntitlementsFileName = "RemoteExplorer.entitlements";
        private const string MulticastNetworkingEntitlement = "com.apple.developer.networking.multicast";

        [PostProcessBuild(999)]
        public static void ConfigureLocalNetworkAccess(BuildTarget target, string pathToBuiltProject)
        {
            if (target != BuildTarget.iOS)
            {
                return;
            }

            AddLocalNetworkUsageDescription(pathToBuiltProject);
            AddMulticastNetworkingEntitlement(pathToBuiltProject);
        }

        private static void AddLocalNetworkUsageDescription(string pathToBuiltProject)
        {
            var plistPath = Path.Combine(pathToBuiltProject, "Info.plist");
            var plist = new PlistDocument();
            plist.ReadFromFile(plistPath);
            plist.root.SetString(
                "NSLocalNetworkUsageDescription",
                "Remote Explorer uses the local network to discover and control your browser server.");
            plist.WriteToFile(plistPath);
        }

        private static void AddMulticastNetworkingEntitlement(string pathToBuiltProject)
        {
            var projectPath = PBXProject.GetPBXProjectPath(pathToBuiltProject);
            var project = new PBXProject();
            project.ReadFromFile(projectPath);

            var mainTargetGuid = project.GetUnityMainTargetGuid();
            var entitlementsProjectPath = project.GetBuildPropertyForAnyConfig(
                mainTargetGuid,
                "CODE_SIGN_ENTITLEMENTS");
            if (string.IsNullOrEmpty(entitlementsProjectPath))
            {
                entitlementsProjectPath = EntitlementsFileName;
                project.AddFile(entitlementsProjectPath, entitlementsProjectPath);
                project.SetBuildProperty(
                    mainTargetGuid,
                    "CODE_SIGN_ENTITLEMENTS",
                    entitlementsProjectPath);
            }

            var entitlementsPath = Path.Combine(pathToBuiltProject, entitlementsProjectPath);
            var entitlementsDirectory = Path.GetDirectoryName(entitlementsPath);
            if (!string.IsNullOrEmpty(entitlementsDirectory))
            {
                Directory.CreateDirectory(entitlementsDirectory);
            }

            var entitlements = new PlistDocument();
            if (File.Exists(entitlementsPath))
            {
                entitlements.ReadFromFile(entitlementsPath);
            }
            else
            {
                entitlements.Create();
            }

            entitlements.root.SetBoolean(MulticastNetworkingEntitlement, true);
            entitlements.WriteToFile(entitlementsPath);
            project.WriteToFile(projectPath);
        }
    }
}
#endif
