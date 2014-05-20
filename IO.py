import os

import cv2
import numpy as np
import myCsoundAudioOptions

def video(state, camera, projector):
    print 'VIDEO PID', os.getpid()
    cv2.namedWindow('Output', cv2.WINDOW_NORMAL)
    video_feed = cv2.VideoCapture(0)
    frame_size = (160, 90)
    
    while True:
        _, frame = video_feed.read()
        frame = cv2.resize(frame, frame_size)
        gray_image = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if state['record']:
            camera.append(np.ndarray.flatten(gray_image)/255.)

        try:
            cv2.imshow('Output', cv2.resize(np.reshape(projector.popleft(),
                                                       frame_size[::-1]), (640,360)))
        except:
            cv2.imshow('Output', np.random.rand(360,640))

        cv2.waitKey(100)

def audio(state, mic, speaker):
    print 'AUDIO PID', os.getpid()
    import csnd6
    cs = csnd6.Csound()
    arguments = csnd6.CsoundArgVList()
    arguments.Append("dummy")
    arguments.Append("self_dot.csd")
    csoundCommandline = myCsoundAudioOptions.myAudioDevices
    comlineParmsList = csoundCommandline.split(' ')
    for item in comlineParmsList:
        arguments.Append("%s"%item)
    cs.Compile(arguments.argc(), arguments.argv())
    stopflag = 0
    
    #fft_audio_in1 = csnd6.PVSDATEXT()
    
    fftsize = int(cs.GetChannel("fftsize"))
    fftin_amptab = 1
    fftin_freqtab = 2
    fftout_amptab = 3
    fftout_freqtab = 4
    fftresyn_amptab = 11
    fftresyn_freqtab = 12
    
    # optimizations to avoid function lookup inside loop
    tGet = cs.TableGet 
    tSet = cs.TableSet
    cGet = cs.GetChannel
    cSet = cs.SetChannel
    perfKsmps = cs.PerformKsmps
    fftbinindices = range(fftsize)
    fftin_amptabs = []
    for i in range(fftsize): fftin_amptabs.append(fftin_amptab)
    fftin_freqtabs = []
    for i in range(fftsize): fftin_freqtabs.append(fftin_freqtab)
    fftout_amptabs = []
    for i in range(fftsize): fftout_amptabs.append(fftout_amptab)
    fftout_freqtabs = []
    for i in range(fftsize): fftout_freqtabs.append(fftout_freqtab)
    fftresyn_amptabs = []
    for i in range(fftsize): fftresyn_amptabs.append(fftresyn_amptab)
    fftresyn_freqtabs = []
    for i in range(fftsize): fftresyn_freqtabs.append(fftresyn_freqtab)


    while not stopflag:
        stopflag = perfKsmps()
        fftinFlag = cGet("pvsinflag")
        fftoutFlag = cGet("pvsoutflag")
        
        if fftinFlag:
            fftin_amplist = map(tGet,fftin_amptabs,fftbinindices)
            fftin_freqlist = map(tGet,fftin_freqtabs,fftbinindices)
            #bogusamp = map(tSet,fftresyn_amptabs,fftbinindices,fftin_amplist)
            #bogusfreq = map(tSet,fftresyn_freqtabs,fftbinindices,fftin_freqlist)
        if fftoutFlag:
            fftout_amplist = map(tGet,fftout_amptabs,fftbinindices)
            fftout_freqlist = map(tGet,fftout_freqtabs,fftbinindices)
        
        # get Csound channel data
        audioStatus = cGet("audioStatus")

        if state['playfile']:
            print '[self.] wants to play {}'.format(state['playfile'])
            print '{}'.format(state['playfile'])
            cs.InputMessage('i3 0 5 "%s"'%'{}'.format(state['playfile']))
            state['playfile'] = False
        
        if state['record']:
            mic.append([cGet("level1"), 
                        cGet("envelope1"), 
                        cGet("pitch1ptrack"), 
                        cGet("pitch1pll"), 
                        cGet("centroid1"),
                        cGet("autocorr1"), 
                        cGet("spread1"), 
                        cGet("skewness1"), 
                        cGet("kurtosis1"), 
                        cGet("flatness1"), 
                        cGet("crest1"), 
                        cGet("flux1"), 
                        cGet("epochSig1"), 
                        cGet("epochRms1"), 
                        cGet("epochZCcps1")])
            # can we do like this ?
            '''
            mic.append(fftin_amplist)
            mic.append(fftin_freqlist)
            '''

        try:
            sound = speaker.popleft()
            cSet("respondLevel1", sound[0])
            cSet("respondEnvelope1", sound[1])
            #cSet("respondPitch1ptrack", sound[2])
            cSet("respondPitch1pll", sound[3])
            cSet("respondCentroid1", sound[4])
            # test partikkel generator
            cSet("partikkel1_amp", sound[0])
            cSet("partikkel1_grainrate", sound[2])
            cSet("partikkel1_wavfreq", sound[4])
            cSet("partikkel1_graindur", sound[6]+0.1)
            
            '''
            # partikkel parameters ready to be set
            partikkelparmOffset = 5
            cSet("partikkel1_amp",sound[partikkelparmOffset+0])
            cSet("partikkel1_grainrate",sound[partikkelparmOffset+1])
            cSet("partikkel1_graindur",sound[partikkelparmOffset+2])
            cSet("partikkel1_sustain",sound[partikkelparmOffset+3])
            cSet("partikkel1_adratio",sound[partikkelparmOffset+4])
            cSet("partikkel1_wavfreq",sound[partikkelparmOffset+5])
            cSet("partikkel1_octaviation",sound[partikkelparmOffset+6])
            cSet("partikkel1_async_amount",sound[partikkelparmOffset+7])
            cSet("partikkel1_distribution",sound[partikkelparmOffset+8])
            cSet("partikkel1_randomask",sound[partikkelparmOffset+9])
            cSet("partikkel1_grFmFreq",sound[partikkelparmOffset+10])
            cSet("partikkel1_grFmIndex",sound[partikkelparmOffset+11])
            cSet("partikkel1_wavekey1",sound[partikkelparmOffset+12])
            cSet("partikkel1_wavekey2",sound[partikkelparmOffset+13])
            cSet("partikkel1_wavekey3",sound[partikkelparmOffset+14])
            cSet("partikkel1_wavekey4",sound[partikkelparmOffset+15])
            cSet("partikkel1_pitchFmFreq",sound[partikkelparmOffset+16])
            cSet("partikkel1_pitchFmIndex",sound[partikkelparmOffset+17])
            cSet("partikkel1_trainPartials",sound[partikkelparmOffset+18])
            cSet("partikkel1_trainChroma",sound[partikkelparmOffset+19])
            cSet("partikkel1_wavemorf",sound[partikkelparmOffset+20])
            '''
            '''
            # spectral parameters ready to be set
            spectralparmOffset = 25
            for i in range(fftsize):
                cs.TableSet(fftresyn_amptab, i, sound[spectralparmOffset+(i*2)])
                cs.TableSet(fftresyn_freqtab, i, sound[spectralparmOffset+(i*2)+1])
            '''                            
        except:
            cSet("respondLevel1", 0)
            cSet("respondEnvelope1", 0)
            cSet("respondPitch1ptrack", 0)
            cSet("respondPitch1pll", 0)
            cSet("respondCentroid1", 0)
            # partikkel test
            cSet("partikkel1_amp", 0)
            cSet("partikkel1_grainrate", 0)
            cSet("partikkel1_wavfreq", 0)
