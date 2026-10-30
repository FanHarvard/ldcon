"""Phase-independent competing fringe peaks with an explicit FSR calibration.

Peak coordinates are observations, not configured windows. A calibrated full
fringe period prevents equal secondary peaks from aliasing into a half period.
"""
import math
import numpy as np
from ldcon.core.contracts import DeviceError


def _peaks(y):
    return np.flatnonzero((y[1:-1]>y[:-2]) & (y[1:-1]>=y[2:]))+1


def _shape(values):
    profile=np.asarray(values,float)
    profile=np.roll(profile,-int(np.argmax(profile)))
    profile=(np.roll(profile,1)+profile+np.roll(profile,-1))/3
    profile=np.roll(profile,-int(np.argmax(profile)))
    profile=profile/profile.max()
    noise=1.4826*np.median(np.abs(np.roll(profile,1)-2*profile+np.roll(profile,-1)))/math.sqrt(6)
    gate=max(.01,6*float(noise))
    level=profile.min()+.5*(profile.max()-profile.min())
    right=next((i for i in range(1,128) if profile[i]<level),128)
    left=next((i for i in range(1,128) if profile[-i]<level),128)
    peaks=[]
    for i in _peaks(np.r_[profile[-1],profile,profile[0]])-1:
        i=int(i)
        if i==0:continue
        l=i-1
        while l>0 and profile[l]<=profile[i]:l-=1
        rr=i+1
        while rr<256 and profile[rr%256]<=profile[i]:rr+=1
        base=max(float(np.min(profile[l:i+1])),float(np.min(np.r_[profile[i:],profile[0]] if rr>=256 else profile[i:rr+1])))
        prominence=float(profile[i]-base)
        if prominence>=gate:
            peaks.append(dict(phase=i/256,height=float(profile[i]),prominence=prominence,near_main=bool(i<=right or i>=256-left)))
    peaks.sort(key=lambda p:p['prominence'],reverse=True)
    return dict(profile=profile,peaks=peaks,width=(left+right)/256,noise=float(noise),gate=gate)


def analyze(pixels, reference_period_px, tolerance_fraction=.12):
    raw=np.asarray(pixels,float)
    if len(raw)<120 or not np.isfinite(raw).all():raise DeviceError('Invalid optical profile')
    if not math.isfinite(reference_period_px) or reference_period_px<8:raise DeviceError('Full-period calibration is required')
    if not 0<tolerance_fraction<.3:raise DeviceError('Invalid period tolerance')
    y=np.convolve(raw,np.ones(3)/3,mode='same')[2:-2]
    y=y-np.percentile(y,2)
    if y.max()<=0:raise DeviceError('No optical fringe contrast')
    centered=y-y.mean();ref=reference_period_px
    lags=np.arange(max(3,int(ref*(1-tolerance_fraction))),min(len(y)//4,int(ref*(1+tolerance_fraction)))+1)
    if not len(lags):raise DeviceError('Invalid full-period calibration')
    correlation=np.array([np.dot(centered[:-lag],centered[lag:])/max(1e-20,np.linalg.norm(centered[:-lag])*np.linalg.norm(centered[lag:])) for lag in lags])
    k=int(np.argmax(correlation));period=float(lags[k])
    if 0<k<len(lags)-1:
        a,b,c=correlation[k-1:k+2];den=a-2*b+c
        if den:period+=float(.5*(a-c)/den)
    candidates=_peaks(y);mains=[]
    ranked=sorted(candidates,key=lambda j:y[j],reverse=True)
    high=sorted(int(i) for i in ranked if y[i]>=.8*y.max())
    high_gaps=np.diff(high)
    height_period=float(np.median(high_gaps)) if len(high_gaps) else None
    height_mad=float(np.median(abs(high_gaps-height_period))) if len(high_gaps) else None
    for i in sorted(candidates,key=lambda j:y[j],reverse=True):
        if y[i]<.3*y.max():break
        if all(abs(i-j)>=.65*period for j in mains):mains.append(int(i))
    total_intervals=max(0,len(mains)-1)
    profiles=[];gaps=[];grid=np.arange(256)/256
    for i,j in zip(sorted(mains)[:-1],sorted(mains)[1:]):
        gap=j-i;height=.5*(y[i]+y[j])
        if .85*period<gap<1.15*period and height>=.3*y.max():
            profiles.append(np.interp(grid,np.arange(gap+1)/gap,y[i:j+1])/height);gaps.append(gap)
    if len(profiles)<3:raise DeviceError('Insufficient qualified full fringe periods')
    shapes=[_shape(p) for p in profiles]
    mean_shape=_shape(np.mean(profiles,axis=0))
    profile=mean_shape['profile'];gate=mean_shape['gate']
    peaks=mean_shape['peaks']
    all_peaks=[p for shape in [mean_shape,*shapes] for p in shape['peaks']]
    inner=max((p['prominence'] for p in all_peaks if p['near_main']),default=0.)
    # Each physical period participates in certification: averaging must not
    # erase a strong secondary peak present in only part of the frame.
    qualified=bool(0<k<len(lags)-1 and correlation[k]>=.5 and abs(period/ref-1)<=tolerance_fraction)
    coverage=len(profiles)/max(1,total_intervals)
    gap_mad=float(np.median(abs(np.asarray(gaps)-np.median(gaps))))/period
    qualified=qualified and coverage>=.5 and gap_mad<=.08
    period_qualified=qualified
    qualified=qualified and inner==0.
    metrics=dict(fringe_interval_coverage_loss=1-coverage,fringe_gap_mad_fraction=gap_mad,near_main_peak_prominence_max=inner,profile_noise_max=max(p['noise'] for p in shapes),F2=float(np.mean(profile)),side_peak_height_max=max((p['height'] for p in all_peaks),default=0.),side_peak_prominence_max=max((p['prominence'] for p in all_peaks),default=0.),side_peak_count=float(len(peaks)),main_fwhm_fraction=max(p['width'] for p in [mean_shape,*shapes]),fringe_period_px=period,fringe_period_correlation=float(correlation[k]))
    for n in (0,1):
        p=peaks[n] if n<len(peaks) else dict(height=0.,prominence=0.,phase=0.)
        metrics.update({f'side_peak_{n+1}_{key}':float(p[key]) for key in ('height','prominence','phase')})
    return dict(metrics=metrics,qualified=qualified,peaks=peaks,profile=profile.tolist(),phase=grid.tolist(),period_reference_px=ref,period_qualified=period_qualified,shape_resolved=bool(inner==0.),per_period=[dict(width=p['width'],peaks=p['peaks'],noise=p['noise']) for p in shapes],periods_used=len(profiles),total_intervals=total_intervals,rejected_intervals=total_intervals-len(profiles),interval_coverage=coverage,interval_gap_mad_fraction=gap_mad,prominence_detection_floor=gate,height_rank_peak_positions=high,height_rank_spacing_median_px=height_period,height_rank_spacing_mad_px=height_mad,revision='full-period-free-position-v3')
