using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using UnityEngine;

namespace RemoteExplorer
{
    // 低开销文件诊断，避免 Unity Console/stack trace 周期性卡主线程。
    // Low-cost file diagnostics; avoids Unity Console and stack-trace stalls on the main thread.
    internal static class RemoteExplorerDiagnostics
    {
        private static readonly object Sync = new object();
        private static readonly Queue<string> PendingLines = new Queue<string>();
        private static AutoResetEvent signal;
        private static Thread writerThread;
        private static StreamWriter writer;
        private static bool initialized;
        private static bool stopping;

        public static string LogPath { get; private set; }

        public static void Initialize()
        {
            lock (Sync)
            {
                if (initialized)
                {
                    return;
                }

                // 普通日志不采集调用栈，保留 warning/error 的诊断价值。
                // Disable stack traces for normal logs while keeping warnings/errors useful.
                Application.SetStackTraceLogType(LogType.Log, StackTraceLogType.None);
                var directory = Application.persistentDataPath;
                Directory.CreateDirectory(directory);
                LogPath = Path.Combine(directory, "remote-explorer-stream.log");
                writer = new StreamWriter(new FileStream(LogPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
                {
                    AutoFlush = false
                };
                signal = new AutoResetEvent(false);
                stopping = false;
                initialized = true;
                writerThread = new Thread(WriteLoop)
                {
                    IsBackground = true,
                    Name = "RemoteExplorerDiagnostics"
                };
                writerThread.Start();
            }

            Info("Diagnostics initialized path=" + LogPath);
            Debug.Log("[RemoteExplorer] Diagnostics log: " + LogPath);
        }

        public static void Info(string message)
        {
            if (!initialized)
            {
                return;
            }

            // 只入队，实际磁盘 I/O 在后台线程完成。 / Enqueue only; disk I/O happens on the writer thread.
            lock (Sync)
            {
                PendingLines.Enqueue(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + " " + message);
            }

            signal?.Set();
        }

        public static void Flush()
        {
            signal?.Set();
        }

        private static void WriteLoop()
        {
            while (true)
            {
                signal.WaitOne(1000);
                Drain();
                lock (Sync)
                {
                    if (stopping && PendingLines.Count == 0)
                    {
                        writer?.Flush();
                        return;
                    }
                }
            }
        }

        private static void Drain()
        {
            while (true)
            {
                string line;
                lock (Sync)
                {
                    if (PendingLines.Count == 0)
                    {
                        writer?.Flush();
                        return;
                    }

                    line = PendingLines.Dequeue();
                }

                writer?.WriteLine(line);
            }
        }
    }
}
