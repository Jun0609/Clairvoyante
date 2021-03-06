import sys
import os
import time
import argparse
import param
import logging
import pickle
import numpy as np
from threading import Thread

logging.basicConfig(format='%(message)s', level=logging.INFO)

def Run(args):
    # create a Clairvoyante
    logging.info("Initializing model ...")
    if args.v2 == True:
        import utils_v2 as utils
        if args.slim == True:
            import clairvoyante_v2_slim as cv
        else:
            import clairvoyante_v2 as cv
    elif args.v3 == True:
        import utils_v2 as utils # v3 network is using v2 utils
        if args.slim == True:
            import clairvoyante_v3_slim as cv
        else:
            import clairvoyante_v3 as cv
    utils.SetupEnv()
    m = cv.Clairvoyante()
    m.init()

    if args.chkpnt_fn != None:
        m.restoreParameters(os.path.abspath(args.chkpnt_fn))
    TrainAll(args, m, utils)


def TrainAll(args, m, utils):
    logging.info("Loading the training dataset ...")
    if args.bin_fn != None:
        with open(args.bin_fn, "rb") as fh:
            total = pickle.load(fh)
            XArrayCompressed = pickle.load(fh)
            YArrayCompressed = pickle.load(fh)
            posArrayCompressed = pickle.load(fh)
    else:
        total, XArrayCompressed, YArrayCompressed, posArrayCompressed = \
        utils.GetTrainingArray(args.tensor_fn,
                               args.var_fn,
                               args.bed_fn)

    logging.info("The size of training dataset: {}".format(total))

    # Op to write logs to Tensorboard
    if args.olog_dir != None:
        summaryWriter = m.summaryFileWriter(args.olog_dir)

    # Train and save the parameters, we train on the first 90% variant sites and validate on the last 10% variant sites
    logging.info("Start training ...")
    logging.info("Learning rate: %.2e" % m.setLearningRate(args.learning_rate))
    logging.info("L2 regularization lambda: %.2e" % m.setL2RegularizationLambda(args.lambd))


    validationLosses = []

    # Model Constants
    trainingStart = time.time()
    trainingTotal = int(total*param.trainingDatasetPercentage)
    validationStart = trainingTotal + 1
    numValItems = total - validationStart
    maxLearningRateSwitch = param.maxLearningRateSwitch

    # Variables reset per epoch
    batchSize = param.trainBatchSize
    epochStart = time.time()
    trainLossSum = 0
    validationLossSum = 0
    datasetPtr = 0

    # Variables reset per learning rate decay
    c = 0;

    i = 1 if args.chkpnt_fn == None else int(args.chkpnt_fn[-param.parameterOutputPlaceHolder:])+1
    XBatch, XNum, XEndFlag = utils.DecompressArray(XArrayCompressed, datasetPtr, batchSize, total)
    YBatch, YNum, YEndFlag = utils.DecompressArray(YArrayCompressed, datasetPtr, batchSize, total)
    datasetPtr += XNum
    while i < param.maxEpoch:
        threadPool = []
        if datasetPtr < validationStart:
            threadPool.append(Thread(target=m.trainNoRT, args=(XBatch, YBatch, )))
        elif datasetPtr >= validationStart:
            threadPool.append(Thread(target=m.getLossNoRT, args=(XBatch, YBatch, )))

        for t in threadPool: t.start()

        if datasetPtr < validationStart and (validationStart - datasetPtr) < param.trainBatchSize:
            batchSize = validationStart - datasetPtr
        elif datasetPtr < validationStart:
            batchSize = param.trainBatchSize
        elif datasetPtr >= validationStart and (datasetPtr % param.predictBatchSize) != 0:
            batchSize = param.predictBatchSize - (datasetPtr % param.predictBatchSize)
        elif datasetPtr >= validationStart:
            batchSize = param.predictBatchSize

        XBatch2, XNum2, XEndFlag2 = utils.DecompressArray(XArrayCompressed, datasetPtr, batchSize, total)
        YBatch2, YNum2, YEndFlag2 = utils.DecompressArray(YArrayCompressed, datasetPtr, batchSize, total)
        if XNum2 != YNum2 or XEndFlag2 != YEndFlag2:
            sys.exit("Inconsistency between decompressed arrays: %d/%d" % (XNum, YNum))

        for t in threadPool: t.join()

        XBatch = XBatch2; YBatch = YBatch2
        if datasetPtr < validationStart:
            trainLossSum += m.trainLossRTVal
            summary = m.trainSummaryRTVal
            if args.olog_dir != None:
                summaryWriter.add_summary(summary, i)
        elif datasetPtr >= validationStart:
            validationLossSum += m.getLossLossRTVal
        datasetPtr += XNum2

        if XEndFlag2 != 0:
            validationLossSum += m.getLoss( XBatch, YBatch )
            logging.info(" ".join([str(i), "Training loss:", str(trainLossSum/trainingTotal), "Validation loss: ", str(validationLossSum/numValItems)]))
            logging.info("Epoch time elapsed: %.2f s" % (time.time() - epochStart))
            validationLosses.append( (validationLossSum, i) )
            # Output the model
            if args.ochk_prefix != None:
                parameterOutputPath = "%s-%%0%dd" % ( args.ochk_prefix, param.parameterOutputPlaceHolder )
                m.saveParameters(os.path.abspath(parameterOutputPath % i))
            # Adaptive learning rate decay
            c += 1
            flag = 0
            if c >= 6:
                if validationLosses[-6][0] - validationLosses[-5][0] > 0:
                    if validationLosses[-5][0] - validationLosses[-4][0] < 0:
                        if validationLosses[-4][0] - validationLosses[-3][0] > 0:
                            if validationLosses[-3][0] - validationLosses[-2][0] < 0:
                                if validationLosses[-2][0] - validationLosses[-1][0] > 0:
                                    flag = 1
                elif validationLosses[-6][0] - validationLosses[-5][0] < 0:
                    if validationLosses[-5][0] - validationLosses[-4][0] > 0:
                        if validationLosses[-4][0] - validationLosses[-3][0] < 0:
                            if validationLosses[-3][0] - validationLosses[-2][0] > 0:
                                if validationLosses[-2][0] - validationLosses[-1][0] < 0:
                                    flag = 1
                else:
                    flag = 1
            if flag == 1:
                maxLearningRateSwitch -= 1
                if maxLearningRateSwitch == 0:
                  break
                logging.info("New learning rate: %.2e" % m.setLearningRate())
                logging.info("New L2 regularization lambda: %.2e" % m.setL2RegularizationLambda())
                c = 0
            # Reset per epoch variables
            i += 1
            trainLossSum = 0; validationLossSum = 0; datasetPtr = 0; epochStart = time.time(); batchSize = param.trainBatchSize
            XBatch, XNum, XEndFlag = utils.DecompressArray(XArrayCompressed, datasetPtr, batchSize, total)
            YBatch, YNum, YEndFlag = utils.DecompressArray(YArrayCompressed, datasetPtr, batchSize, total)
            datasetPtr += XNum

    logging.info("Training time elapsed: %.2f s" % (time.time() - trainingStart))

    # show the parameter set with the smallest validation loss
    validationLosses.sort()
    i = validationLosses[0][1]
    logging.info("Best validation loss at batch: %d" % i)

    logging.info("Testing on the training and validation dataset ...")
    predictStart = time.time()
    predictBatchSize = param.predictBatchSize
    if args.v2 == True or args.v3 == True:
        datasetPtr = 0
        XBatch, _, _ = utils.DecompressArray(XArrayCompressed, datasetPtr, predictBatchSize, total)
        bases = []; zs = []; ts = []; ls = []
        base, z, t, l = m.predict(XBatch)
        bases.append(base); zs.append(z); ts.append(t); ls.append(l)
        datasetPtr += predictBatchSize
        while datasetPtr < total:
            XBatch, _, endFlag = utils.DecompressArray(XArrayCompressed, datasetPtr, predictBatchSize, total)
            base, z, t, l = m.predict(XBatch)
            bases.append(base); zs.append(z); ts.append(t); ls.append(l)
            datasetPtr += predictBatchSize
            if endFlag != 0:
                break
        bases = np.concatenate(bases[:]); zs = np.concatenate(zs[:]); ts = np.concatenate(ts[:]); ls = np.concatenate(ls[:])
    logging.info("Prediciton time elapsed: %.2f s" % (time.time() - predictStart))

    # Evaluate the trained model
    YArray, _, _ = utils.DecompressArray(YArrayCompressed, 0, total, total)
    if args.v2 == True or args.v3 == True:
        logging.info("Version 2 model, evaluation on base change:")
        allBaseCount = top1Count = top2Count = 0
        for predictV, annotateV in zip(bases, YArray[:,0:4]):
            allBaseCount += 1
            sortPredictV = predictV.argsort()[::-1]
            if np.argmax(annotateV) == sortPredictV[0]: top1Count += 1; top2Count += 1
            elif np.argmax(annotateV) == sortPredictV[1]: top2Count += 1
        logging.info("all/top1/top2/top1p/top2p: %d/%d/%d/%.2f/%.2f" %\
                    (allBaseCount, top1Count, top2Count, float(top1Count)/allBaseCount*100, float(top2Count)/allBaseCount*100))
        logging.info("Version 2 model, evaluation on Zygosity:")
        ed = np.zeros( (2,2), dtype=np.int )
        for predictV, annotateV in zip(zs, YArray[:,4:6]):
            ed[np.argmax(annotateV)][np.argmax(predictV)] += 1
        for i in range(2):
            logging.info("\t".join([str(ed[i][j]) for j in range(2)]))
        logging.info("Version 2 model, evaluation on variant type:")
        ed = np.zeros( (4,4), dtype=np.int )
        for predictV, annotateV in zip(ts, YArray[:,6:10]):
            ed[np.argmax(annotateV)][np.argmax(predictV)] += 1
        for i in range(4):
            logging.info("\t".join([str(ed[i][j]) for j in range(4)]))
        logging.info("Version 2 model, evaluation on indel length:")
        ed = np.zeros( (6,6), dtype=np.int )
        for predictV, annotateV in zip(ls, YArray[:,10:16]):
            ed[np.argmax(annotateV)][np.argmax(predictV)] += 1
        for i in range(6):
            logging.info("\t".join([str(ed[i][j]) for j in range(6)]))


def main():
    parser = argparse.ArgumentParser(
            description="Train Clairvoyante" )

    parser.add_argument('--bin_fn', type=str, default = None,
            help="Binary tensor input generated by tensor2Bin.py, tensor_fn, var_fn and bed_fn will be ignored")

    parser.add_argument('--tensor_fn', type=str, default = "vartensors",
            help="Tensor input")

    parser.add_argument('--var_fn', type=str, default = "truthvars",
            help="Truth variants list input")

    parser.add_argument('--bed_fn', type=str, default = None,
            help="High confident genome regions input in the BED format")

    parser.add_argument('--chkpnt_fn', type=str, default = None,
            help="Input a checkpoint for testing or continue training")

    parser.add_argument('--learning_rate', type=float, default = param.initialLearningRate,
            help="Set the initial learning rate, default: %(default)s")

    parser.add_argument('--lambd', type=float, default = param.l2RegularizationLambda,
            help="Set the l2 regularization lambda, default: %(default)s")

    parser.add_argument('--ochk_prefix', type=str, default = None,
            help="Prefix for checkpoint outputs at each learning rate change, optional")

    parser.add_argument('--olog_dir', type=str, default = None,
            help="Directory for tensorboard log outputs, optional")

    parser.add_argument('--v3', type=param.str2bool, nargs='?', const=True, default = True,
            help="Use Clairvoyante version 3")

    parser.add_argument('--v2', type=param.str2bool, nargs='?', const=True, default = False,
            help="Use Clairvoyante version 2")

    parser.add_argument('--slim', type=param.str2bool, nargs='?', const=True, default = False,
            help="Train using the slim version of Clairvoyante, optional")

    args = parser.parse_args()

    if len(sys.argv[1:]) == 0:
        parser.print_help()
        sys.exit(1)

    Run(args)


if __name__ == "__main__":
    main()
