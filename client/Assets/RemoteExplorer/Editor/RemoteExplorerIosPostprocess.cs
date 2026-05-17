#if UNITY_EDITOR && UNITY_IOS
using System.IO;
using UnityEditor;
using UnityEditor.Callbacks;
using UnityEditor.iOS.Xcode;

namespace RemoteExplorer.Editor
{
    public static class RemoteExplorerIosPostprocess
    {
        [PostProcessBuild]
        public static void AddLocalNetworkUsage(BuildTarget target, string pathToBuiltProject)
        {
            if (target != BuildTarget.iOS)
            {
                return;
            }

            var plistPath = Path.Combine(pathToBuiltProject, "Info.plist");
            var plist = new PlistDocument();
            plist.ReadFromFile(plistPath);
            plist.root.SetString(
                "NSLocalNetworkUsageDescription",
                "Remote Explorer uses the local network to discover and control your browser server.");
            plist.WriteToFile(plistPath);
        }
    }
}
#endif
