using System;

namespace RemoteExplorer
{
    public class RemoteStreamFrame
    {
        public uint FrameId;
        public int Width;
        public int Height;
        public int SourceWidth;
        public int SourceHeight;
        public byte[] JpegData;
    }

    internal class RemoteStreamFrameBuilder
    {
        private readonly bool[] receivedChunkFlags;
        private readonly byte[] frameBuffer;
        private readonly int chunkPayloadBytes;
        private int receivedChunks;
        private int expectedBytes = -1;

        public uint FrameId { get; private set; }
        public int Width { get; private set; }
        public int Height { get; private set; }
        public int SourceWidth { get; private set; }
        public int SourceHeight { get; private set; }
        public int ChunkCount => receivedChunkFlags.Length;
        public DateTime LastUpdatedUtc { get; private set; }

        public RemoteStreamFrameBuilder(
            uint frameId,
            int chunkCount,
            int chunkPayloadBytes,
            int width,
            int height,
            int sourceWidth,
            int sourceHeight)
        {
            FrameId = frameId;
            Width = width;
            Height = height;
            SourceWidth = sourceWidth;
            SourceHeight = sourceHeight;
            this.chunkPayloadBytes = chunkPayloadBytes;
            receivedChunkFlags = new bool[chunkCount];
            frameBuffer = new byte[chunkCount * chunkPayloadBytes];
            LastUpdatedUtc = DateTime.UtcNow;
        }

        public bool AddChunk(int index, byte[] packet, int offset, int length)
        {
            if (index < 0 ||
                index >= receivedChunkFlags.Length ||
                receivedChunkFlags[index] ||
                packet == null ||
                offset < 0 ||
                length <= 0 ||
                offset + length > packet.Length ||
                length > chunkPayloadBytes)
            {
                return false;
            }

            var destinationOffset = index * chunkPayloadBytes;
            if (destinationOffset + length > frameBuffer.Length)
            {
                return false;
            }

            Buffer.BlockCopy(packet, offset, frameBuffer, destinationOffset, length);
            receivedChunkFlags[index] = true;
            receivedChunks++;
            if (index == receivedChunkFlags.Length - 1)
            {
                expectedBytes = destinationOffset + length;
            }

            LastUpdatedUtc = DateTime.UtcNow;
            return receivedChunks == receivedChunkFlags.Length && expectedBytes > 0;
        }

        public RemoteStreamFrame Build()
        {
            if (expectedBytes <= 0)
            {
                throw new InvalidOperationException("Stream frame is incomplete.");
            }

            for (var i = 0; i < receivedChunkFlags.Length; i++)
            {
                if (!receivedChunkFlags[i])
                {
                    throw new InvalidOperationException("Stream frame is incomplete.");
                }
            }

            var image = new byte[expectedBytes];
            Buffer.BlockCopy(frameBuffer, 0, image, 0, expectedBytes);
            return new RemoteStreamFrame
            {
                FrameId = FrameId,
                Width = Width,
                Height = Height,
                SourceWidth = SourceWidth,
                SourceHeight = SourceHeight,
                JpegData = image
            };
        }
    }
}
