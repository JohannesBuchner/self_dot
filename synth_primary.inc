
; ******************************
; basic subtractive harmonic synth
	instr 51			

	krms1 		chnget "respondLevel1"
	;kcps1 		chnget "respondPitch1ptrack"
	kcps1 		chnget "respondPitch1pll"
	kcentro1 	chnget "respondCentroid1"

	krms1		limit krms1, 0, 1
	krms1		mediank krms1, 100, 100
	krms1		tonek krms1, 20

	kcps1		limit kcps1, 20, 2000
	kcps1		tonek kcps1, 20
	kcentro1	limit kcentro1, 20, 2000
	kcentro1	tonek kcentro1, 20

	; simple subtractive synth, creating N harmonic bands from pitch info,
	; using centroid to further filter which of the harmonics are more prominent
	anoise		rnd31 1,1
	afilt1a		butterbp anoise, kcps1, kcps1*0.05
	afilt1b		butterbp anoise, kcps1*2, kcps1*0.05
	afilt1c		butterbp anoise, kcps1*3, kcps1*0.05
	afilt1d		butterbp anoise, kcps1*4, kcps1*0.05
	afilt1e		butterbp anoise, kcps1*5, kcps1*0.05
	asum		sum afilt1a, afilt1b, afilt1c, afilt1d, afilt1e
	aout		butterbp asum*5+(anoise*0.01), kcentro1, kcentro1*0.2
	aout		= aout*krms1*10
			chnmix aout, "MasterOut1"
			chnmix aout, "MasterOut2"
/*
	acps		upsamp kcps1/2000
	acentro		upsamp kcentro1/2000
	arms		upsamp krms1
			fout "testplay.wav", 14, aout, acps, acentro, arms, aenv
*/
	endin

; ******************************
; partikkel instr 
	instr 52			
;krms1 		chnget "respondLevel1"
;kactive = (krms1 > 0 ? 1 : 0)
;printk2 kactive  

#include "partikkel_chn.inc"
#include "partikkel_self.inc"
			chnmix a1, "MasterOut1"
			chnmix a2, "MasterOut2"
	endin

; ******************************
; spectral resynthesis instr 
	instr 53			
	kinit		init 1
	if kinit > 0 then
	a1		= 0
	fsin 		pvsanal	a1, gifftsize, gifftsize/4, gifftsize, 1
	endif
	;fsin		pvsinit gifftsize, gifftsize/4, gifftsize, 1
			pvsftr	fsin,gifnaResyn,gifnfResyn		;read modified data back to fsrc
	aout		pvsynth	fsin				;and resynth
			chnmix aout, "MasterOut1"
			chnmix aout, "MasterOut2"
	endin