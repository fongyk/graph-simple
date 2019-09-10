from PACK import *
from torch.optim.lr_scheduler import StepLR

from encoder import Encoder
from model import SupervisedGraphSAGE_Single
from utils import buildTestData
from collect_graph import removeIsolated, collectGraph_train, collectGraph_train_v2, collectGraph_test

import numpy as np
import math
import time
import random
import visdom
from tqdm import tqdm

import os

import argparse
import ast

eval_func = '/data4/fong/oxford5k/evaluation/compute_ap'
retrieval_result = '/data4/fong/pytorch/Graph/retrieval'
test_dataset = {
    'oxf': {
        'node_num': 5063,
        'img_testpath': '/data4/fong/pytorch/RankNet/building/test_oxf/images',
        'feature_path': '/data4/fong/pytorch/Graph/test_feature_map/oxford',
        'gt_path': '/data4/fong/oxford5k/oxford5k_groundTruth',
    },
    'par': {
        'node_num': 6392,
        'img_testpath': '/data4/fong/pytorch/RankNet/building/test_par/images',
        'feature_path': '/data4/fong/pytorch/Graph/test_feature_map/paris',
        'gt_path': '/data4/fong/paris6k/paris_groundTruth',
    }
}
building_oxf = buildTestData(img_path=test_dataset['oxf']['img_testpath'], gt_path=test_dataset['oxf']['gt_path'], eval_func=eval_func)
building_par = buildTestData(img_path=test_dataset['par']['img_testpath'], gt_path=test_dataset['par']['gt_path'], eval_func=eval_func)
building = {
    'oxf': building_oxf,
    'par': building_par,
}

def makeModel(node_num, class_num, feature_map, adj_lists, args):
    ## feature embedding
    embedding = nn.Embedding(node_num, args.feat_dim)
    embedding.weight = nn.Parameter(torch.from_numpy(feature_map).float(), requires_grad=False)

    ## single-layer encoder
    encoder = Encoder(embedding, args.feat_dim, args.embed_dim, adj_lists, num_sample=args.num_sample, gcn=args.use_gcn, use_cuda=args.use_cuda)

    ## model
    graphsage = SupervisedGraphSAGE_Single(class_num, encoder)
    if args.use_cuda:
        embedding.cuda()
        encoder.cuda()
        graphsage.cuda()

    return graphsage

def train(checkpoint_path, round, args):
    ## load training data
    print "loading training data ......"
    # node_num, class_num = removeIsolated(args.suffix)
    node_num, class_num = 33792, 569
    label, feature_map, adj_lists = collectGraph_train_v2(node_num, class_num, args.feat_dim, args.num_sample, args.suffix, round)

    graphsage = makeModel(node_num, class_num, feature_map, adj_lists, args)

    # if checkpoint_path is not None:
    #     checkpoint = torch.load(checkpoint_path)
    #     graphsage_state_dict = graphsage.state_dict()
    #     graphsage_state_dict.update({'weight': checkpoint['graph_state_dict']['weight']})
    #     graphsage_state_dict.update({'encoder.weight': checkpoint['graph_state_dict']['encoder.weight']})
    #     graphsage.load_state_dict(graphsage_state_dict)

    # optimizer = torch.optim.SGD(filter(lambda para: para.requires_grad, graphsage.parameters()), lr=args.learning_rate)
    optimizer = torch.optim.Adam(
        filter(lambda para: para.requires_grad, graphsage.parameters()), lr=args.learning_rate*(0.8**round),
    )
    scheduler = StepLR(optimizer, step_size=args.step_size, gamma=0.5)

    ## train
    np.random.seed(2)
    random.seed(2)
    rand_indices = np.random.permutation(node_num)
    train_nodes = list(rand_indices[:args.train_num])
    val_nodes = list(rand_indices[args.train_num:])
    # for nt in train_nodes:
    #     for vt in val_nodes:
    #         if vt in adj_lists[nt]:
    #             adj_lists[nt].remove(vt)

    epoch_num = args.epoch_num
    batch_size = args.batch_size
    iter_num = int(math.ceil(args.train_num / float(batch_size)))
    check_loss = []
    val_accuracy = []
    check_step = args.check_step
    train_loss = 0.0
    iter_cnt = 0
    for e in range(epoch_num):
        graphsage.train()
        scheduler.step()

        random.shuffle(train_nodes)
        for batch in range(iter_num):
            batch_nodes = train_nodes[batch*batch_size: (batch+1)*batch_size]
            batch_label = torch.LongTensor(label[batch_nodes])
            if args.use_cuda:
                batch_label = batch_label.cuda()
            optimizer.zero_grad()
            loss = graphsage.loss(batch_nodes, batch_label)
            loss.backward()
            optimizer.step()
            iter_cnt += 1
            train_loss += loss.cpu().item()
            if iter_cnt % check_step == 0:
                check_loss.append(train_loss/check_step)
                print time.strftime('%Y-%m-%d %H:%M:%S'), "epoch: {}, iter: {}, loss:{:.4f}".format(e, iter_cnt, train_loss/check_step)
                train_loss = 0.0

        ## validation
        graphsage.eval()
        group = int(math.ceil(len(val_nodes)/float(batch_size)))
        val_cnt = 0
        for batch in range(group):
            batch_nodes = val_nodes[batch*batch_size: (batch+1)*batch_size-1]
            batch_label = label[batch_nodes].squeeze()
            _, score = graphsage(batch_nodes)
            batch_predict = np.argmax(score.cpu().data.numpy(), axis=1)
            val_cnt += np.sum(batch_predict == batch_label)
        val_accuracy.append(val_cnt/float(len(val_nodes)))
        print time.strftime('%Y-%m-%d %H:%M:%S'), "Epoch: {}, Validation Accuracy: {:.4f}".format(e, val_cnt/float(len(val_nodes)))
        print "******" * 10

    ## extract new feature
    graphsage.eval()
    new_feature_map = torch.FloatTensor()
    batch_num = int(math.ceil(node_num/float(batch_size)))
    for batch in tqdm(range(batch_num)):
        start_node = batch*args.batch_size
        end_node = min((batch+1)*args.batch_size, node_num)
        test_nodes = range(start_node, end_node)
        new_feature, _ = graphsage(test_nodes)
        new_feature = F.normalize(new_feature, p=2, dim=0)
        new_feature_map = torch.cat((new_feature_map, new_feature.t().cpu().data), dim=0)
    new_feature_map = new_feature_map.numpy()
    np.save('/data4/fong/pytorch/Graph/train_feature_map/feature_map_{}.npy'.format(round+1), new_feature_map)

    checkpoint_path = 'checkpoint/checkpoint_single_{}.pth'.format(time.strftime('%Y%m%d%H%M'))
    torch.save({
            'train_num': args.train_num,
            'epoch_num': args.epoch_num,
            'learning_rate': args.learning_rate,
            'embed_dim': args.embed_dim,
            'num_sample': args.num_sample,
            'use_gcn': args.use_gcn,
            'graph_state_dict': graphsage.state_dict(),
            'optimizer': optimizer.state_dict(),
            },
            checkpoint_path)

    vis = visdom.Visdom(env='Graph', port='8099')
    vis.line(
            X = np.arange(1, len(check_loss)+1, 1) * check_step,
            Y = np.array(check_loss),
            opts = dict(
                title=time.strftime('%Y-%m-%d %H:%M:%S') + ', single, gcn {}'.format(args.use_gcn),
                xlabel='itr.',
                ylabel='loss'
            )
    )
    vis.line(
            X = np.arange(1, len(val_accuracy)+1, 1),
            Y = np.array(val_accuracy),
            opts = dict(
                title=time.strftime('%Y-%m-%d %H:%M:%S') + ', single, gcn {}'.format(args.use_gcn),
                xlabel='epoch',
                ylabel='accuracy'
            )
    )

    return checkpoint_path, class_num

def test(checkpoint_path, class_num, round, args):
    for key in building.keys():
        node_num = test_dataset[key]['node_num']
        old_feature_map, adj_lists = collectGraph_test(test_dataset[key]['feature_path'], node_num, args.feat_dim, args.num_sample, args.suffix, round)

        graphsage = makeModel(node_num, class_num, old_feature_map, adj_lists, args)

        checkpoint = torch.load(checkpoint_path)
        graphsage_state_dict = graphsage.state_dict()
        graphsage_state_dict.update({'encoder.weight': checkpoint['graph_state_dict']['encoder.weight']})
        graphsage_state_dict.update({'fc.weight': checkpoint['graph_state_dict']['fc.weight']})
        graphsage_state_dict.update({'fc.bias': checkpoint['graph_state_dict']['fc.bias']})
        graphsage.load_state_dict(graphsage_state_dict)
        graphsage.eval()

        batch_num = int(math.ceil(node_num/float(args.batch_size)))
        new_feature_map = torch.FloatTensor()
        for batch in tqdm(range(batch_num)):
            start_node = batch*args.batch_size
            end_node = min((batch+1)*args.batch_size, node_num)
            test_nodes = range(start_node, end_node)
            new_feature, _ = graphsage(test_nodes)
            new_feature = F.normalize(new_feature, p=2, dim=0)
            new_feature_map = torch.cat((new_feature_map, new_feature.t().cpu().data), dim=0)
        new_feature_map = new_feature_map.numpy()
        np.save(os.path.join(test_dataset[key]['feature_path'], 'feature_map_{}.npy'.format(round+1)), new_feature_map)

        old_similarity = np.dot(old_feature_map, old_feature_map.T)
        new_similarity = np.dot(new_feature_map, new_feature_map.T)
        mAP_old = building[key].evalRetrieval(old_similarity, retrieval_result)
        mAP_new = building[key].evalRetrieval(new_similarity, retrieval_result)
        print time.strftime('%Y-%m-%d %H:%M:%S'), 'eval {}'.format(key)
        print 'base feature: {}, new feature: {}'.format(old_feature_map.shape, new_feature_map.shape)
        print 'base mAP: {:.4f}, new mAP: {:.4f}, improve: {:.4f}'.format(mAP_old, mAP_new, mAP_new-mAP_old)

        meanAggregator = graphsage.encoder.aggregator
        ## directly update node's features by mean pooling features of its neighbors.
        mean_feature_map = torch.FloatTensor()
        for batch in tqdm(range(batch_num)):
            start_node = batch*args.batch_size
            end_node = min((batch+1)*args.batch_size, node_num)
            test_nodes = range(start_node, end_node)
            mean_feature = meanAggregator(test_nodes, [adj_lists[i] for i in test_nodes], args.num_sample)
            mean_feature = F.normalize(mean_feature, p=2, dim=1)
            mean_feature_map = torch.cat((mean_feature_map, mean_feature.cpu().data), dim=0)
        mean_feature_map = mean_feature_map.numpy()
        mean_similarity = np.dot(mean_feature_map, mean_feature_map.T)
        mAP_mean = building[key].evalRetrieval(mean_similarity, retrieval_result)
        print 'mean aggregation mAP: {:.4f}'.format(mAP_mean)
        print ""

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description = 'Supervised single-layer GraphSAGE, train on Landmark_clean, test on Oxford5k and Paris6k.')
    parser.add_argument('-E', '--epoch_num', type=int, default=70, required=False, help='training epoch number.')
    parser.add_argument('-R', '--step_size', type=int, default=30, required=False, help='learning rate decay step_size.')
    parser.add_argument('-B', '--batch_size', type=int, default=128, required=False, help='training batch size.')
    parser.add_argument('-S', '--check_step', type=int, default=50, required=False, help='loss check step.')
    parser.add_argument('-C', '--use_cuda', type=ast.literal_eval, default=True, required=False, help='whether to use gpu (True) or not (False).')
    parser.add_argument('-G', '--use_gcn', type=ast.literal_eval, default=True, required=False, help='whether to use gcn (True) or not (False).')
    parser.add_argument('-L', '--learning_rate', type=float, default=0.005, required=False, help='training learning rate.')
    parser.add_argument('-N', '--num_sample', type=int, default=10, required=False, help='number of neighbors to aggregate.')
    parser.add_argument('-x', '--suffix', type=str, default='.frmac.npy', required=False, help='feature type, \'f\' for vggnet (512-d), \'fr\' for resnet (2048-d), \'frmac\' for vgg16_rmac (512-d).')
    parser.add_argument('-f', '--feat_dim', type=int, default=512, required=False, help='input feature dim of node.')
    parser.add_argument('-d', '--embed_dim', type=int, default=512, required=False, help='embedded feature dim of encoder.')
    parser.add_argument('-T', '--train_num', type=int, default=25000, required=False, help='number of training nodes (less than 36460). Left for validation.')
    parser.add_argument('-O', '--round', type=int, default=1, required=False, help='number of updating features and graph')
    args, _ = parser.parse_known_args()
    print "< < < < < < < < Supervised Single-layer GraphSAGE > > > > > > >"
    print "= = = = = = = = = = = PARAMETERS SETTING = = = = = = = = = = ="
    print "round:", args.round
    print "epoch_num:", args.epoch_num
    print "step_size:", args.step_size
    print "batch_size:", args.batch_size
    print "check_step:", args.check_step
    print "train_num:", args.train_num
    print "learning_rate:", args.learning_rate
    print "suffix:", args.suffix
    print "feat_dim:", args.feat_dim
    print "embed_dim:", args.embed_dim
    print "num_sample:", args.num_sample
    print "use_cuda:", args.use_cuda
    print "use_gcn:", args.use_gcn
    print "= = = = = = = = = = = = = = = = = = = = = = = = = = = = = = = ="

    checkpoint_path = None
    for r in range(args.round):
        print "Round", r
        checkpoint_path, class_num = train(checkpoint_path, r, args)
        test(checkpoint_path, class_num, r, args)
