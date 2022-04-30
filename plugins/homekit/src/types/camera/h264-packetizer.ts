import { RtpHeader, RtpPacket } from "../../../../../external/werift/packages/rtp/src/rtp/rtp";

// https://yumichan.net/video-processing/video-compression/introduction-to-h264-nal-unit/
const NAL_TYPE_STAP_A = 24;
const NAL_TYPE_FU_A = 28;
const NAL_TYPE_IDR = 5;
const NAL_TYPE_SEI = 6;
const NAL_TYPE_SPS = 7;
const NAL_TYPE_PPS = 8;

const NAL_HEADER_SIZE = 1;
const FU_A_HEADER_SIZE = 2;
const LENGTH_FIELD_SIZE = 2;
const STAP_A_HEADER_SIZE = NAL_HEADER_SIZE + LENGTH_FIELD_SIZE;


// a stap a packet is a packet that aggregates multiple nals
function depacketizeStapA(data: Buffer) {
    const ret: Buffer[] = [];
    let lastPos: number;
    let pos = NAL_HEADER_SIZE;
    while (pos < data.length) {
        if (lastPos !== undefined)
            ret.push(data.subarray(lastPos, pos));
        const naluSize = data.readUInt16BE(pos);
        pos += LENGTH_FIELD_SIZE;
        lastPos = pos;
        pos += naluSize;
    }
    ret.push(data.subarray(lastPos));
    return ret;
}

export class H264Repacketizer {
    extraPackets = 0;
    fuaMax: number;
    pendingStapA: RtpPacket[];
    pendingFuA: RtpPacket[];
    seenSps = false;

    constructor(public maxPacketSize: number, public codecInfo: {
        sps: Buffer,
        pps: Buffer,
    }) {
        // 12 is the rtp/srtp header size.
        this.fuaMax = maxPacketSize - FU_A_HEADER_SIZE;;
    }

    // a fragmentation unit (fua) is a NAL unit broken into multiple fragments.
    // https://datatracker.ietf.org/doc/html/rfc6184#section-5.8
    packetizeFuA(data: Buffer, noStart?: boolean, noEnd?: boolean): Buffer[] {
        // handle both normal packets and fua packets.
        // a fua packet can be fragmented easily into smaller packets, as
        // it is already a fragment, and splitting segments is
        // trivial.

        const initialNalType = data[0] & 0x1f;

        if (initialNalType === NAL_TYPE_FU_A) {
            const fnri = data[0] & (0x80 | 0x60);
            const originalNalType = data[1] & 0x1f;
            const isFuStart = !!(data[1] & 0x80);
            const isFuEnd = !!(data[1] & 0x40);
            const isFuMiddle = !isFuStart && !isFuEnd;

            const originalNalHeader = Buffer.from([fnri | originalNalType]);
            data = Buffer.concat([originalNalHeader, data.subarray(FU_A_HEADER_SIZE)]);

            if (isFuStart) {
                noEnd = true;
            }
            else if (isFuEnd) {
                noStart = true;
            }
            else if (isFuMiddle) {
                noStart = true;
                noEnd = true;
            }
        }

        const payloadSize = data.length - NAL_HEADER_SIZE;
        const numPackets = Math.ceil(payloadSize / this.fuaMax);
        let numLargerPackets = payloadSize % numPackets;
        const packageSize = Math.floor(payloadSize / numPackets);

        const fnri = data[0] & (0x80 | 0x60);
        const nal = data[0] & 0x1F;

        const fuIndicator = fnri | NAL_TYPE_FU_A;

        const fuHeaderMiddle = Buffer.from([fuIndicator, nal]);
        const fuHeaderStart = noStart ? fuHeaderMiddle : Buffer.from([fuIndicator, nal | 0x80]);
        const fuHeaderEnd = noEnd ? fuHeaderMiddle : Buffer.from([fuIndicator, nal | 0x40]);
        let fuHeader = fuHeaderStart;

        const packages: Buffer[] = [];
        let offset = NAL_HEADER_SIZE;

        while (offset < data.length) {
            let payload: Buffer;
            if (numLargerPackets > 0) {
                numLargerPackets -= 1;
                payload = data.subarray(offset, offset + packageSize + 1);
                offset += packageSize + 1;
            }
            else {
                payload = data.subarray(offset, offset + packageSize);
                offset += packageSize;
            }

            if (offset === data.length) {
                fuHeader = fuHeaderEnd;
            }

            packages.push(Buffer.concat([fuHeader, payload]));

            fuHeader = fuHeaderMiddle;
        }

        return packages;
    }

    // https://datatracker.ietf.org/doc/html/rfc6184#section-5.7.1
    packetizeOneStapA(datas: Buffer[]): Buffer {
        const payload: Buffer[] = [];

        if (!datas.length)
            throw new Error('packetizeOneStapA requires at least one NAL');

        let counter = 0;
        let availableSize = this.maxPacketSize - STAP_A_HEADER_SIZE;

        let stapHeader = NAL_TYPE_STAP_A | (datas[0][0] & 0xE0);

        while (datas.length && datas[0].length + LENGTH_FIELD_SIZE <= availableSize && counter < 9) {
            const nalu = datas.shift();

            stapHeader |= nalu[0] & 0x80;

            const nri = nalu[0] & 0x60;
            if ((stapHeader & 0x60) < nri)
                stapHeader = stapHeader & 0x9F | nri;

            availableSize -= LENGTH_FIELD_SIZE + nalu.length;
            counter += 1;
            const packed = Buffer.alloc(2);
            packed.writeUInt16BE(nalu.length, 0);
            payload.push(packed, nalu);
        }

        // is this possible?
        if (counter === 0) {
            console.warn('stap a packet is too large. this may be a bug.');
            return datas.shift();
        }

        payload.unshift(Buffer.from([stapHeader]));
        return Buffer.concat(payload);
    }

    packetizeStapA(datas: Buffer[]) {
        const ret: Buffer[] = [];
        while (datas.length) {
            ret.push(this.packetizeOneStapA(datas));
        }
        return ret;
    }

    createPacket(rtp: RtpPacket, data: Buffer, marker: boolean) {
        const originalSequenceNumber = rtp.header.sequenceNumber;
        const originalMarker = rtp.header.marker;
        const originalPayload = rtp.payload;
        rtp.header.sequenceNumber = (rtp.header.sequenceNumber + this.extraPackets + 0x10000) % 0x10000;
        rtp.header.marker = marker;
        rtp.payload = data;
        const ret = rtp.serialize();
        rtp.header.sequenceNumber = originalSequenceNumber;
        rtp.header.marker = originalMarker;
        rtp.payload = originalPayload;
        if (data.length > this.maxPacketSize)
            console.warn('packet exceeded max packet size. this may a bug.');
        return ret;
    }

    flushPendingStapA(ret: Buffer[]) {
        if (!this.pendingStapA)
            return;
        const first = this.pendingStapA[0];
        const hadMarker = first.header.marker;

        const aggregates = this.packetizeStapA(this.pendingStapA.map(packet => packet.payload));
        if (aggregates.length !== 1) {
            console.error('expected only 1 packet for sps/pps stapa');
            this.pendingStapA = undefined;
            return;
        }

        aggregates.forEach((packetized, index) => {
            const marker = hadMarker && index === aggregates.length - 1;
            ret.push(this.createPacket(first, packetized, marker));
        });

        this.extraPackets -= this.pendingStapA.length - 1;
        this.pendingStapA = undefined;
    }

    flushPendingFuA(ret: Buffer[]) {
        if (!this.pendingFuA)
            return;

        // defragmenting assumes packets are sorted by sequence number,
        // and are all available, which is guaranteed over rtsp/tcp, but not over rtp/udp.
        const first = this.pendingFuA[0];
        const last = this.pendingFuA[this.pendingFuA.length - 1];

        const hasFuStart = !!(first.payload[1] & 0x80);
        const hasFuEnd = !!(last.payload[1] & 0x40);

        const originalNalType = first.payload[1] & 0x1f;
        let lastSequenceNumber: number;
        for (const packet of this.pendingFuA) {
            const nalType = packet.payload[1] & 0x1f;
            if (nalType !== originalNalType) {
                console.error('nal type mismatch');
                this.pendingFuA = undefined;
                return;
            }
            if (lastSequenceNumber !== undefined) {
                if (packet.header.sequenceNumber !== (lastSequenceNumber + 1) % 0x10000) {
                    console.error('fua packet is missing. skipping refragmentation.');
                    this.pendingFuA = undefined;
                    return;
                }
            }
            lastSequenceNumber = packet.header.sequenceNumber;
        }

        const fnri = first.payload[0] & (0x80 | 0x60);
        const originalNalHeader = Buffer.from([fnri | originalNalType]);

        const originalFragments = this.pendingFuA.map(packet => packet.payload.subarray(FU_A_HEADER_SIZE));
        originalFragments.unshift(originalNalHeader);
        const defragmented = Buffer.concat(originalFragments);

        const fragments = this.packetizeFuA(defragmented, !hasFuStart, !hasFuEnd);
        const hadMarker = last.header.marker;
        this.createRtpPackets(first, fragments, ret, hadMarker);

        this.extraPackets -= this.pendingFuA.length - 1;

        this.pendingFuA = undefined;
    }

    createRtpPackets(packet: RtpPacket, nalus: Buffer[], ret: Buffer[], hadMarker = packet.header.marker) {
        nalus.forEach((packetized, index) => {
            if (index !== 0)
                this.extraPackets++;
            const marker = hadMarker && index === nalus.length - 1;
            ret.push(this.createPacket(packet, packetized, marker));
        });
    }

    maybeSendSpsPps(packet: RtpPacket, ret: Buffer[]) {
        if (!this.codecInfo.sps || !this.codecInfo.pps)
            return;

        const aggregates = this.packetizeStapA([this.codecInfo.sps, this.codecInfo.pps]);
        if (aggregates.length !== 1) {
            console.error('expected only 1 packet for sps/pps stapa');
            return;
        }
        this.createRtpPackets(packet, aggregates, ret);
        this.extraPackets++;
    }

    repacketize(packet: RtpPacket): Buffer[] {
        const ret: Buffer[] = [];
        const nalType = packet.payload[0] & 0x1F;

        // fragmented packets must share a timestamp
        if (this.pendingFuA && this.pendingFuA[0].header.timestamp !== packet.header.timestamp) {
            this.flushPendingFuA(ret);
        }

        // stapa packets must share the same timestamp
        if (this.pendingStapA && this.pendingStapA[0].header.timestamp !== packet.header.timestamp) {
            this.flushPendingStapA(ret);
        }

        if (nalType === NAL_TYPE_FU_A) {
            // fua may share a timestamp as stapa, but don't aggregated with stapa
            this.flushPendingStapA(ret);

            const data = packet.payload;
            const originalNalType = data[1] & 0x1f;
            const isFuStart = !!(data[1] & 0x80);
            // if this is an idr frame, but no sps has been sent, dummy one up.
            // the stream may not contain sps.
            if (originalNalType === NAL_TYPE_IDR && isFuStart && !this.seenSps) {
                this.maybeSendSpsPps(packet, ret);
            }

            if (!this.pendingFuA) {
                // the fua packet may already fit, in which case we could just send it.
                // but for some reason that doesn't work??
                if (false && packet.payload.length <= this.maxPacketSize) {
                    const isFuEnd = !!(data[1] & 0x40);
                    ret.push(this.createPacket(packet, packet.payload, packet.header.marker && isFuEnd));
                }
                else if (packet.payload.length >= this.maxPacketSize * 2) {
                    // most rtsp implementations send fat fua packets ~64k. can just repacketize those
                    // with minimal extra packet overhead.
                    const fragments = this.packetizeFuA(packet.payload);
                    this.createRtpPackets(packet, fragments, ret);
                }
                else {
                    // the fua packet is an unsuitable size and needs to be defragmented
                    // and refragmented.
                    this.pendingFuA = [];
                }
            }

            if (this.pendingFuA) {
                this.pendingFuA.push(packet);

                const isFuEnd = !!(packet.payload[1] & 0x40);
                if (isFuEnd)
                    this.flushPendingFuA(ret);
            }
        }
        else if (nalType === NAL_TYPE_STAP_A) {
            this.flushPendingFuA(ret);

            // break the aggregated packet up and send it.
            const depacketized = depacketizeStapA(packet.payload)
                .filter(payload => {
                    const nalType = payload[0] & 0x1F;
                    this.seenSps = this.seenSps || (nalType === NAL_TYPE_SPS);
                    // SEI nal causes homekit to fail
                    return nalType !== NAL_TYPE_SEI;
                });
            const aggregates = this.packetizeStapA(depacketized);
            this.createRtpPackets(packet, aggregates, ret);
        }
        else if (nalType >= 1 && nalType < 24) {
            this.flushPendingFuA(ret);

            // codec information should be aggregated. usually around 50 bytes total.
            if (nalType === NAL_TYPE_SPS || nalType === NAL_TYPE_PPS) {
                this.seenSps = this.seenSps || (nalType === NAL_TYPE_SPS);
                if (!this.pendingStapA)
                    this.pendingStapA = [];
                this.pendingStapA.push(packet);
                return ret;
            }

            this.flushPendingStapA(ret);

            // SEI nal causes homekit to fail
            if (nalType === NAL_TYPE_SEI) {
                this.extraPackets--;
                return ret;
            }

            if (nalType === NAL_TYPE_IDR && !this.seenSps) {
                // if this is an idr frame, but no sps has been sent, dummy one up.
                // the stream may not contain sps.
                this.maybeSendSpsPps(packet, ret);
            }

            if (packet.payload.length > this.maxPacketSize) {
                const fragments = this.packetizeFuA(packet.payload);
                this.createRtpPackets(packet, fragments, ret);
            }
            else {
                // can send this packet as is!
                ret.push(this.createPacket(packet, packet.payload, packet.header.marker));
            }
        }
        else {
            console.error('unknown nal unit type ' + nalType);
            this.extraPackets--;
        }

        return ret;
    }
}